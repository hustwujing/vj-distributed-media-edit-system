"""Deterministic workflow controller. No model or media execution in this process."""
from __future__ import annotations
import json
import os
import signal
import threading
import uuid
from . import workflow_graph as graph,workflow_planning as planning
from .workflow_graph import encode,digest,TERMINAL,AuthorizationRequired


class ReconcileLost(RuntimeError):pass
class InvalidProviderHandle(ValueError):pass


class Orchestrator:
    def __init__(self,store,owner=None,ttl=15):
        self.store=store;self.owner=owner or uuid.uuid4().hex;self.ttl=ttl
        self.stop=threading.Event()

    def claim_due(self,limit=100):
        limit=max(1,min(100,int(limit)))
        with self.store.connect() as db:
            now=self.store.now()
            sql="""SELECT id,reconcile_generation FROM workflow_runs WHERE status NOT IN ('succeeded','failed','cancelled')
                AND next_reconcile_at<=? AND reconcile_until<=? ORDER BY next_reconcile_at,id LIMIT ?"""
            if self.store.dsn:sql+=' FOR UPDATE SKIP LOCKED'
            rows=db.execute(sql,(now,now,limit)).fetchall();claims=[]
            for row in rows:
                generation=row['reconcile_generation']+1
                if db.execute('''UPDATE workflow_runs SET reconcile_owner=?,reconcile_until=?,reconcile_generation=?
                    WHERE id=? AND reconcile_until<=? AND reconcile_generation=?''',
                    (self.owner,now+self.ttl,generation,row['id'],now,row['reconcile_generation'])).rowcount:
                    claims.append((row['id'],generation))
            return claims

    def check(self,db,identifier,generation):
        row=db.execute('SELECT reconcile_owner,reconcile_until,reconcile_generation FROM workflow_runs WHERE id=?',(identifier,)).fetchone()
        if not row or row['reconcile_owner']!=self.owner or row['reconcile_generation']!=generation or row['reconcile_until']<=self.store.now():
            raise ReconcileLost('Workflow reconcile lease expired or replaced')

    def reconcile(self,identifier,generation):
        store=self.store
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.check(db,identifier,generation)
            run=graph.get(store,identifier)
            if run['status'] in TERMINAL:return
            self.commands(run)
            run=graph.get(store,identifier)
            user_paused=run['status']=='paused' and run['terminal_reason']=='user_paused'
            self.accept_results(run)
            run=graph.get(store,identifier)
            self.recover(run)
            run=graph.get(store,identifier)
            if user_paused and run['status'] not in ('cancelling',*TERMINAL):
                self.set_status(run,'paused','user_paused');run=graph.get(store,identifier)
            if run['status']=='cancelling':self.cancel(run)
            elif run['status'] not in ('paused','waiting_user',*TERMINAL):
                self.apply_planner(run)
                run=graph.get(store,identifier)
                if run['status'] not in ('paused','waiting_user',*TERMINAL):
                    self.advance(run)
            self.check(db,identifier,generation)
            db.execute('''UPDATE workflow_runs SET next_reconcile_at=?,reconcile_until=0,reconcile_owner='',updated_at=?
                WHERE id=? AND reconcile_owner=? AND reconcile_generation=?''',
                (store.now()+1,store.now(),identifier,self.owner,generation))

    def set_status(self,run,status,reason=''):
        self.store.execute('UPDATE workflow_runs SET status=?,terminal_reason=?,updated_at=? WHERE id=?',
                           (status,reason,self.store.now(),run['id']))
        if run['status']!=status:graph.event(self.store,run,'state_changed',{'status':status,'reason':reason})

    def decision(self,run,kind,request):
        store=self.store
        current=store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND kind=? AND status='pending'",(run['id'],kind))
        if not current:
            frozen={'graph_version':run['graph_version'],'authorization_version':run['authorization_version'],'request':request}
            store.execute('''INSERT INTO workflow_decisions(id,workflow_id,kind,graph_version,authorization_version,
                snapshot_digest,request_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)''',
                (uuid.uuid4().hex,run['id'],kind,run['graph_version'],run['authorization_version'],digest(frozen),encode(request),store.now(),store.now()))
        self.set_status(run,'waiting_user',kind)

    def open_repair(self,run,subject,source,evidence='',error_code=''):
        context={'active':True,'subject_task':subject,'subject_revision':next((t['revision'] for t in run['tasks'] if t['id']==subject),0) if subject else None,
                 'source':source,'evidence':evidence,'error_code':error_code,'opened_graph_version':run['graph_version']}
        self.store.execute('UPDATE workflow_runs SET repair_context_json=?,updated_at=? WHERE id=?',
                           (encode(context),self.store.now(),run['id']))
        graph.event(self.store,run,'repair_context_opened',{'task_id':subject,'error_code':error_code,'source':source})
        return context

    def commands(self,run):
        store=self.store
        for command in store.query("SELECT * FROM workflow_commands WHERE workflow_id=? AND status='pending' ORDER BY created_at,id LIMIT 100",(run['id'],)):
            run=graph.get(store,run['id']);payload=json.loads(command['payload_json']);error=''
            if run['status'] in TERMINAL:error='workflow_terminal'
            elif run['status']=='cancelling' and command['action']!='cancel':error='cancellation_in_progress'
            elif command['action']=='cancel':
                self.set_status(run,'cancelling','user_cancelled')
                store.execute("UPDATE agent_turns SET status='cancelling' WHERE id=?",(run['turn_id'],))
                from .task_lifecycle import request_cleanup
                with store.connect() as db:request_cleanup(db,run['turn_id'])
            elif command['action']=='pause':self.set_status(run,'paused','user_paused')
            elif command['action']=='resume':
                unresolved=store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending'",(run['id'],))
                if run['status']!='paused':error='resume_requires_safe_paused_state'
                elif unresolved:self.set_status(run,'waiting_user','user_decision_pending')
                elif any(t['internal'] and t['spec'].get('profile')=='media_failure_diagnostician' and t['status']=='ready' for t in run['tasks']):
                    self.set_status(run,'reviewing')
                elif any(t['error_code'] in ('submission_unknown','result_unavailable') for t in run['tasks']):error='external_result_requires_resolution'
                elif any(t['error_code'] in ('model_result','quality_failed','invalid_result_manifest','pre_submit') for t in run['tasks']):error='replan_required'
                elif any(t['status']=='paused' and not t['internal'] and t['phase']=='execute'
                         and (t['spec'].get('retry_policy')!='safe_once' or t['attempts']>=2) for t in run['tasks']):error='replan_required'
                else:
                    for task in run['tasks']:
                        if task['status']=='paused' and (task['phase'] in ('poll','collect','validate') or not task['internal'] and task['spec'].get('retry_policy')=='safe_once'):
                            store.execute("UPDATE workflow_tasks SET status='retry_wait',next_run_at=?,wait_reason='cleanup',error_code='',error_detail='' WHERE workflow_id=? AND id=?",(store.now(),run['id'],task['id']))
                    self.set_status(run,'running')
            elif command['action']=='answer':
                try:self.answer(run,payload)
                except (ValueError,KeyError,TypeError):error='invalid_or_stale_decision'
            store.execute('UPDATE workflow_commands SET status=?,error_code=?,applied_at=? WHERE id=?',
                          ('rejected' if error else 'applied',error,store.now(),command['id']))
            graph.event(store,run,'command_processed',{'command_id':command['id'],'action':command['action'],'error_code':error})

    def answer(self,run,payload):
        store=self.store
        payload=graph.DecisionAnswer.model_validate(payload).model_dump(exclude_none=True)
        rows=store.query("SELECT * FROM workflow_decisions WHERE id=? AND workflow_id=? AND status='pending'",(payload.get('decision_id'),run['id']))
        if not rows or run['status']!='waiting_user':raise ValueError('Decision not pending')
        row=rows[0]
        if row['graph_version']!=run['graph_version'] or row['authorization_version']!=run['authorization_version'] or payload.get('snapshot_digest')!=row['snapshot_digest']:
            raise ValueError('Decision changed')
        if payload.get('action')=='reject':
            if row['kind']=='quality':
                subject=json.loads(row['request_json'])['task_id']
                store.execute("UPDATE workflow_tasks SET status='paused',error_code='quality_failed',error_detail=? WHERE workflow_id=? AND id=?",
                    ('用户未通过内容验收，请调整要求后重新规划。',run['id'],subject))
            self.set_status(run,'paused','user_rejected')
        elif row['kind']=='plan_change' and payload.get('action')=='approve':
            # This service has no vendor credentials or private backend settings.
            # Freeze the implementation advertised by the actual executors.
            authorization=planning.bind_scope(store,payload.get('authorization'))
            owned={asset['id'] for asset in store.assets(run['project_id'])}
            if not set(authorization.allowed_assets)<=owned:raise ValueError('Foreign asset')
            if authorization.content_fingerprint!=graph.content_fingerprint(store,run['project_id']):raise ValueError('Changed input')
            # Approval cannot silently invalidate contracts already committed.
            probe={**run,'authorization':authorization.model_dump(),'sealed':False,'status':'running'}
            if any(not task['internal'] for task in run['tasks']):
                graph.validate_proposal(store,probe,{'proposal_id':'authorization-check','base_graph_version':run['graph_version'],
                    'reason':'Validate accepted scope','sealed':True})
            store.execute('UPDATE workflow_runs SET authorization_json=?,authorization_version=authorization_version+1,goal=? WHERE id=?',
                          (encode(authorization.model_dump()),authorization.goal,run['id']))
            store.execute("UPDATE workflow_decisions SET status='superseded',updated_at=? WHERE workflow_id=? AND id<>? AND status='pending'",
                          (store.now(),run['id'],row['id']))
            self.set_status(run,'running')
        elif row['kind']=='quality' and payload.get('action')=='approve':
            request=json.loads(row['request_json']);task=next(t for t in run['tasks'] if t['id']==request['task_id'])
            if task['revision']!=request['revision'] or not task['quality'].get('technical_valid'):raise ValueError('Technical acceptance failed')
            if task['quality'].get('requires_agent') and not task['quality'].get('agent_review_passed'):raise ValueError('Agent review has not passed')
            store.execute("UPDATE workflow_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE workflow_id=? AND id=?",(run['id'],task['id']))
            self.set_status(run,'running')
        else:raise ValueError('Unsupported answer')
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
        if payload.get('action')!='reject':
            if store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending'",(run['id'],)):
                self.set_status(run,'waiting_user','user_decision_pending')
            elif any(t['status']=='paused' and not t['internal'] for t in graph.get(store,run['id'])['tasks']):
                self.set_status(run,'paused','task_requires_resolution')

    def accept_results(self,run):
        store=self.store
        for result in store.query("SELECT * FROM task_results WHERE workflow_id=? AND status='staged' ORDER BY created_at,id LIMIT 100",(run['id'],)):
            task=next((t for t in run['tasks'] if t['id']==result['task_id']),None)
            leases=store.query('SELECT * FROM execution_leases WHERE attempt=?',(result['lease_id'],))
            accepted=bool(task and leases and task['revision']==result['task_revision'] and task['lease_id']==result['lease_id']
                          and task['lease_generation']==result['lease_generation'] and leases[0]['generation']==result['lease_generation']
                          and run['status'] not in ('cancelling',*TERMINAL) and task['status'] not in TERMINAL)
            if not accepted:
                store.execute("UPDATE task_results SET status='late',committed_at=? WHERE id=?",(store.now(),result['id']));continue
            outcome=result['outcome'];result_status='committed'
            try:
                data=json.loads(result['result_json'])
                if not isinstance(data,dict):raise ValueError('Task result must be an object')
                if outcome=='resource_wait':
                    store.execute("UPDATE workflow_tasks SET status='retry_wait',next_run_at=?,wait_reason='resource',attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END WHERE workflow_id=? AND id=?",
                        (store.now()+5,run['id'],task['id']))
                elif task['internal']:
                    if outcome=='succeeded':
                        if task['spec']['profile']=='media_planner':
                            # The proposal itself is independently immutable and staged.
                            planner=store.query("SELECT status FROM planner_runs WHERE task_id=? AND workflow_id=?",(task['id'],run['id']))
                            if not planner or planner[0]['status']!='staged':outcome='failed';data={'error_code':'missing_proposal'}
                        elif task['spec']['profile']=='media_failure_diagnostician':self.accept_diagnosis(run,task,data)
                        else:self.accept_review(run,task,data)
                    if outcome!='succeeded':self.task_failure(run,task,data)
                elif outcome=='waiting':
                    handle=data.get('external_id') or task['external_id']
                    if not re_full_handle(handle):raise InvalidProviderHandle('Provider submission has no usable handle')
                    store.execute("UPDATE workflow_tasks SET status='waiting',phase='poll',external_id=?,next_run_at=?,wait_reason='provider' WHERE workflow_id=? AND id=?",
                        (handle,store.now()+max(1,min(60,data.get('poll_after',5))),run['id'],task['id']))
                elif outcome=='succeeded':
                    self.accept_artifacts(run,task,data,json.loads(result['artifact_manifest']))
                else:self.task_failure(run,task,data)
            except (ValueError,KeyError,TypeError) as exc:
                result_status='rejected'
                code='submission_unknown' if isinstance(exc,InvalidProviderHandle) else 'invalid_result_manifest'
                self.task_failure(run,task,{'error_code':code})
                graph.event(store,run,'task_result_rejected',{'task_id':task['id'],'error_code':code})
            store.execute('UPDATE task_results SET status=?,committed_at=? WHERE id=?',(result_status,store.now(),result['id']))
            if result_status=='committed':graph.event(store,run,'task_result_committed',{'task_id':task['id'],'revision':task['revision'],'outcome':outcome})

    def accept_artifacts(self,run,task,data,manifest):
        store=self.store;files=data.get('files',[]);report=data.get('quality',{})
        if not files or not isinstance(report,dict):
            self.task_failure(run,task,{'error_code':'technical_validation_failed','phase':'validate'});return
        saved=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                          (run['id'],task['id'],task['revision']))
        authoritative=json.loads(saved[0]['manifest']) if saved else {}
        for name in files:
            record=manifest.get(name)
            if not record or not name.startswith(f'edit/workflows/{run["id"]}/{task["id"]}/'):raise ValueError('Result escaped task output scope')
            known=authoritative.get(name)
            if not known or any(record.get(k)!=known.get(k) for k in ('sha256','size','artifact','location')):
                raise ValueError('Result does not match its durable task checkpoint')
            if store.shared_files:
                artifact=store.artifacts.record(run['project_id'],record['artifact'])
                if artifact['sha256']!=record['sha256'] or artifact['size']!=record['size']:raise ValueError('Artifact integrity mismatch')
            verified=next((f for f in report.get('files',[]) if f['name']==name),None)
            if report.get('technical_valid') and (not verified or verified.get('sha256')!=record['sha256']):raise ValueError('Quality evidence does not match artifact')
        declared=graph.Task.model_validate(task['spec'])
        report.pop('agent_review_passed',None)
        report['requires_review']=any(check.kind=='review' for check in declared.acceptance)
        report['requires_agent']=any(check.kind=='agent' for check in declared.acceptance)
        store.execute('UPDATE workflow_tasks SET candidate_result_json=?,quality_json=?,phase=\'validate\' WHERE workflow_id=? AND id=?',
                      (encode(files),encode(report),run['id'],task['id']))
        if not report.get('technical_valid'):
            self.task_failure(run,task,{'error_code':'technical_validation_failed','phase':'validate'});return
        from .workflow_inputs import roles
        spec=graph.Task.model_validate(task['spec']).execution_spec()
        if spec['kind']=='agent':spec['kind']='script'
        report['output_roles']=roles(spec,files,data.get('output_roles'))
        store.execute('UPDATE workflow_tasks SET quality_json=? WHERE workflow_id=? AND id=?',(encode(report),run['id'],task['id']))
        if not report.get('passed'):
            self.task_failure(run,task,{'error_code':'quality_failed','phase':'validate'});return
        if report.get('requires_agent'):
            store.execute("UPDATE workflow_tasks SET status='reviewing' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
            self.queue_review(graph.get(store,run['id']),task['id'])
        elif report.get('requires_review'):
            store.execute("UPDATE workflow_tasks SET status='reviewing' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
            self.decision(run,'quality',{'task_id':task['id'],'revision':task['revision'],'quality':report})
        else:
            store.execute("UPDATE workflow_tasks SET status='succeeded',accepted_result_json=?,error_code='',error_detail='',wait_reason='' WHERE workflow_id=? AND id=?",(encode(files),run['id'],task['id']))

    def task_failure(self,run,task,data):
        store=self.store;code=data.get('error_code','execution_failed');phase=data.get('phase',task['phase'])
        if phase not in ('execute','poll','collect','validate'):phase=task['phase']
        if code not in {'submission_unknown','result_unavailable','technical_validation_failed','quality_failed','execution_failed',
                        'provider_failed','poll_failed','collect_failed','agent_failed','budget_exhausted','missing_proposal','invalid_result_manifest','model_result','pre_submit'}:code='execution_failed'
        retry=(not task['internal'] and graph.Task.model_validate(task['spec']).retry_policy=='safe_once'
               and task['attempts']<2 and phase=='execute' and code=='execution_failed'
               and data.get('classification') not in ('code','environment','parameters','model_result'))
        retry=retry or bool(task['internal'] and code=='agent_failed' and data.get('transient') and task['attempts']<3)
        message={
            'model_result':'模型未找到符合条件的结果，请调整输入或要求后重新规划。',
            'budget_exhausted':'本轮操作次数或模型预算已达上限。请调整执行范围或预算后重新发起。',
            'agent_failed':'专业 Agent 未能提交有效结果。已保留诊断，请检查后重试或重新规划。',
            'submission_unknown':'外部请求结果尚未确认，已暂停以避免重复调用。',
            'pre_submit':'提交前检查未通过，尚未发起外部请求。请检查输入或配置后重新规划。',
            'result_unavailable':'执行已中断，缺少可安全恢复的结果。请先核实已有请求与产物。',
            'quality_failed':'候选成果未通过验收，已暂停，尚未正式交付。',
            'technical_validation_failed':'候选文件未通过技术检查，已暂停，尚未正式交付。',
            'poll_failed':'查询外部任务状态失败，可以重试查询已有任务。',
            'collect_failed':'获取已有任务结果失败，可以重试收取结果。',
        }.get(code,'任务未完成，已保留执行证据。')
        store.execute('UPDATE workflow_tasks SET status=?,phase=?,error_code=?,error_detail=?,next_run_at=? WHERE workflow_id=? AND id=?',
            ('retry_wait' if retry else 'paused',phase,code,message,store.now()+2,run['id'],task['id']))
        if task['internal'] and retry:
            if task['spec'].get('profile')=='media_planner':
                store.execute("UPDATE planner_runs SET status='pending',error_code='',updated_at=? WHERE workflow_id=? AND task_id=?",
                              (store.now(),run['id'],task['id']))
            else:
                store.execute("UPDATE workflow_reviews SET status='pending',updated_at=? WHERE workflow_id=? AND task_id=?",
                              (store.now(),run['id'],task['id']))
        elif task['internal']:
            store.execute("UPDATE planner_runs SET status='rejected',error_code=?,updated_at=? WHERE workflow_id=? AND task_id=? AND status IN ('pending','running','staged')",(code,store.now(),run['id'],task['id']))
            store.execute("UPDATE workflow_reviews SET status='rejected',updated_at=? WHERE workflow_id=? AND task_id=?",(store.now(),run['id'],task['id']))
        if not retry:
            self.set_status(run,'paused',code)
            if not task['internal'] and task['spec'].get('on_failure')=='diagnose' and code not in ('submission_unknown','budget_exhausted','result_unavailable'):
                self.open_repair(run,task['id'],'execution_failure',error_code=code)
                self.queue_review(graph.get(store,run['id']),task['id'],purpose='diagnose')

    def apply_planner(self,run):
        store=self.store
        for request in store.query("SELECT * FROM planner_runs WHERE workflow_id=? AND status='staged' ORDER BY created_at",(run['id'],)):
            # The worker must have stopped before its proposals become executable.
            active=store.query('SELECT attempt FROM execution_leases WHERE turn_id=? AND slot=? AND active=1 AND expires>?',
                               (run['turn_id'],run['turn_id']+'|'+request['task_id'],store.now()))
            if active:continue
            cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(request['lease_id'],))
            if cleanup:continue
            owner=next(t for t in run['tasks'] if t['id']==request['task_id'])
            if not request['lease_id'] or owner['lease_id']!=request['lease_id'] or owner['lease_generation']!=request['lease_generation']:
                store.execute("UPDATE planner_runs SET status='rejected',error_code='stale_generation' WHERE id=?",(request['id'],))
                store.execute("UPDATE workflow_tasks SET status='failed',error_code='stale_generation' WHERE workflow_id=? AND id=?",(run['id'],request['task_id']))
                graph.event(store,run,'proposal_rejected',{'request_id':request['id'],'error_code':'stale_generation'})
                continue
            try:planning.apply(store,run,request)
            except (AuthorizationRequired,ValueError) as exc:
                code='authorization_required' if isinstance(exc,AuthorizationRequired) else 'invalid_proposal'
                store.execute("UPDATE planner_runs SET status='rejected',error_code=?,updated_at=? WHERE id=?",(code,store.now(),request['id']))
                store.execute("UPDATE workflow_tasks SET status='failed',error_code=? WHERE workflow_id=? AND id=?",(code,run['id'],request['task_id']))
                if isinstance(exc,AuthorizationRequired):self.decision(run,'plan_change',{'planner_run':request['id'],'proposal':json.loads(request['proposal_json']),'reason':str(exc)})
                else:
                    graph.event(store,run,'proposal_rejected',{'request_id':request['id'],'error_code':code})
                    try:planning.queue(store,run,{'rejected':request['id'],'reason':str(exc)[:1000]})
                    except ValueError:self.set_status(run,'paused','planning_budget_exhausted')

    def recover(self,run):
        store=self.store;now=store.now()
        limit=store.query('SELECT deadline_at,termination_reason FROM execution_limits WHERE task_id=?',(run['turn_id'],))
        if limit and (limit[0]['deadline_at']<=now or limit[0]['termination_reason']):
            self.set_status(run,'cancelling','deadline_or_budget')
            store.execute("UPDATE agent_turns SET status='cancelling' WHERE id=?",(run['turn_id'],));return
        for task in run['tasks']:
            if task['status'] not in ('ready','leased','running') or not task['lease_id']:continue
            leases=store.query('SELECT * FROM execution_leases WHERE attempt=?',(task['lease_id'],))
            if leases and leases[0]['active'] and leases[0]['expires']>now:
                if task['status']=='ready':store.execute("UPDATE workflow_tasks SET status='running' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
                continue
            result=store.query('SELECT status FROM task_results WHERE workflow_id=? AND task_id=? AND lease_generation=?',
                               (run['id'],task['id'],task['lease_generation']))
            if result:continue
            cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(task['lease_id'],))
            if cleanup:continue
            if task['internal']:
                staged=store.query("SELECT id FROM planner_runs WHERE workflow_id=? AND task_id=? AND status='staged'",(run['id'],task['id']))
                if staged:continue
                safe=task['attempts']<3
            else:
                from .operation_lifecycle import for_step,RecoveryAction
                spec=graph.Task.model_validate(task['spec']).execution_spec()
                if spec['kind']=='agent':safe=task['spec']['retry_policy']=='safe_once' and task['attempts']<2
                else:
                    implementation=for_step(spec)
                    calls=store.query('SELECT state,result,external_id FROM workflow_provider_calls WHERE request_key=?',
                                      (run['id']+':'+task['id']+':'+str(task['revision']),))
                    if calls and calls[0]['external_id']:
                        store.execute("UPDATE workflow_tasks SET external_id=?,phase='poll',status='ready',lease_id='' WHERE workflow_id=? AND id=?",(calls[0]['external_id'],run['id'],task['id']));continue
                    if calls and calls[0]['state']=='succeeded' and json.loads(calls[0]['result']):
                        store.execute("UPDATE workflow_tasks SET candidate_result_json=?,phase='validate',status='ready',lease_id='' WHERE workflow_id=? AND id=?",(calls[0]['result'],run['id'],task['id']));continue
                    if calls and calls[0]['state']=='succeeded':
                        self.task_failure(run,task,{'error_code':'result_unavailable'});continue
                    if calls:
                        self.task_failure(run,task,{'error_code':'submission_unknown'});continue
                    safe=(task['phase'] in ('poll','collect','validate') or implementation.records_calls or spec['retry_safe']) and task['attempts']<2
            if safe:store.execute("UPDATE workflow_tasks SET status='ready',lease_id='',next_run_at=? WHERE workflow_id=? AND id=?",(now,run['id'],task['id']))
            else:self.task_failure(run,task,{'error_code':'result_unavailable'})

    def advance(self,run):
        store=self.store;now=store.now()
        if store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending'",(run['id'],)):
            self.set_status(run,'waiting_user','user_decision_pending');return
        # Manual reviews are presented one at a time, but parallel candidates
        # must not lose their pending confirmation when the first is answered.
        pending=next((t for t in run['tasks'] if t['status']=='reviewing' and t['quality'].get('requires_review')
                      and (not t['quality'].get('requires_agent') or t['quality'].get('agent_review_passed'))),None)
        if pending:
            self.decision(run,'quality',{'task_id':pending['id'],'revision':pending['revision'],'quality':pending['quality']});return
        done={t['id'] for t in run['tasks'] if t['status']=='succeeded'}
        for task in run['tasks']:
            parents={d['upstream_task_id'] for d in run['dependencies'] if d['downstream_task_id']==task['id']}
            if task['status'] in ('blocked','retry_wait','waiting') and task['next_run_at']<=now and parents<=done:
                if task['lease_id']:
                    active=store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',(task['lease_id'],now))
                    cleaning=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(task['lease_id'],))
                    if active or cleaning:continue
                store.execute("UPDATE workflow_tasks SET status='ready',lease_id='',wait_reason='' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
        active_planners=store.query("SELECT id FROM planner_runs WHERE workflow_id=? AND status IN ('pending','running','staged')",(run['id'],))
        business=[t for t in run['tasks'] if not t['internal']]
        if run['sealed'] and any(t['status']=='paused' for t in business):
            self.set_status(run,'paused','sealed_graph_unresolved');return
        if not run['sealed'] and not active_planners:
            ready=not business or set(run['replan_after'])<=done
            if ready:
                try:planning.queue(store,run,{'completed':sorted(done)})
                except ValueError:self.set_status(run,'paused','planning_budget_exhausted')
        if run['sealed'] and business and all(t['status']=='succeeded' for t in business) and not active_planners:
            busy=store.query("SELECT attempt FROM execution_leases WHERE turn_id=? AND active=1 AND expires>?",(run['turn_id'],now))
            cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['turn_id'],))
            external=store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],))
            decisions=store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending'",(run['id'],))
            if not busy and not cleanup and not external and not decisions:
                reviews=store.query("SELECT * FROM workflow_reviews WHERE workflow_id=? AND purpose='final' AND graph_version=? ORDER BY created_at DESC",(run['id'],run['graph_version']))
                if reviews and reviews[0]['status']=='passed':self.publish(run,reviews[0])
                elif not reviews:self.queue_review(run)

    def queue_review(self,run,subject='',purpose=None):
        store=self.store;purpose=purpose or ('task' if subject else 'final')
        from .specialist_profiles import get as profile
        policy='media_failure_diagnostician' if purpose=='diagnose' else 'visual_quality_reviewer' if subject else 'delivery_reviewer'
        if purpose=='diagnose' and run['authorization']['agent_profile_digests'].get(policy)!=digest(profile(policy).snapshot()):
            self.set_status(run,'paused','diagnostic_policy_not_authorized');return
        existing=store.query("SELECT id FROM workflow_reviews WHERE workflow_id=? AND purpose=? AND subject_task=? AND status IN ('pending','running','staged')",(run['id'],purpose,subject))
        if existing:return
        identifier=uuid.uuid4().hex;task_id='__review_'+identifier[:16];now=store.now()
        frozen={'graph_version':run['graph_version'],'authorization_version':run['authorization_version'],
                'goal':run['goal'],'delivery_targets':run['authorization']['delivery_targets'],'sealed':run['sealed'],
                'tasks':[t for t in run['tasks'] if not t['internal'] and (not subject or t['id']==subject)]}
        frozen['review_scope']=purpose
        if purpose=='task':
            selected=frozen['tasks'][0]
            frozen['goal']=selected['spec']['objective']
            frozen.pop('delivery_targets')
            frozen['review_criteria']=list(dict.fromkeys(a['criterion'] for a in selected['spec']['acceptance']
                if a.get('kind')=='agent' and a.get('criterion'))) or ['满足当前步骤目标及输出契约']
        if purpose=='diagnose':
            frozen['diagnostics']=[json.loads(r['record']) for r in store.query('SELECT record FROM workflow_diagnostics WHERE workflow=? AND step=? ORDER BY created DESC LIMIT 5',(run['id'],subject))]
            checkpoints=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                                    (run['id'],subject,frozen['tasks'][0]['revision']))
            frozen['diagnostic_files']=[name for name in json.loads(checkpoints[0]['manifest']) if name.endswith('/execution.log')] if checkpoints else []
        frozen['upstream']=[]
        if subject:
            selected=next(t for t in run['tasks'] if t['id']==subject)
            for ref in selected['spec'].get('inputs',{}).values():
                if ref.get('step'):
                    parent=next(t for t in run['tasks'] if t['id']==ref['step'])
                    path=parent['quality'].get('output_roles',{}).get(ref['role'])
                    if path and parent['status']=='succeeded':frozen['upstream'].append({**parent,'accepted_result':[path],'candidate_result':[]})
        spec={'profile':policy,'request_id':identifier,
              'snapshot_digest':digest(frozen),'objective':frozen['goal']}
        store.execute('''INSERT INTO workflow_reviews(id,workflow_id,task_id,purpose,subject_task,graph_version,
            snapshot_json,snapshot_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)''',
            (identifier,run['id'],task_id,purpose,subject,run['graph_version'],encode(frozen),digest(frozen),now,now))
        store.execute('''INSERT INTO workflow_tasks(workflow_id,id,type,spec_json,profile_json,status,internal,created_graph_version,created_at,updated_at)
            VALUES (?,?,'agent',?,?,'ready',1,?,?,?)''',(run['id'],task_id,encode(spec),encode(profile(spec['profile']).snapshot()),run['graph_version'],now,now))
        if not subject or purpose=='diagnose':self.set_status(run,'reviewing')
        graph.event(store,run,'review_requested',{'request_id':identifier,'purpose':purpose,'task_id':subject})

    def accept_diagnosis(self,run,task,data):
        from .workflow_agent_contracts import Diagnosis
        store=self.store
        rows=store.query("SELECT * FROM workflow_reviews WHERE id=? AND workflow_id=? AND purpose='diagnose'",(task['spec']['request_id'],run['id']))
        if not rows or data.get('snapshot_digest')!=rows[0]['snapshot_digest']:raise ValueError('Diagnostic snapshot changed')
        diagnosis=Diagnosis.model_validate(data['diagnosis']).model_dump()
        store.execute("UPDATE workflow_reviews SET status='completed',decision_json=?,updated_at=? WHERE id=?",(encode(diagnosis),store.now(),rows[0]['id']))
        store.execute("UPDATE workflow_tasks SET status='succeeded' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
        subject=next(t for t in run['tasks'] if t['id']==rows[0]['subject_task'])
        from .workflow_repair import resolve
        resolution=resolve(subject['error_code'],subject['phase'])
        if resolution.action=='manager_revision':
            self.open_repair(run,subject['id'],'diagnosis',rows[0]['id'],subject['error_code'])
            store.execute('UPDATE workflow_runs SET sealed=0 WHERE id=?',(run['id'],))
            self.set_status(run,'planning','repair_planning')
            planning.queue(store,graph.get(store,run['id']),{'failure_diagnosed':rows[0]['id'],'task_id':subject['id'],
                'revision':subject['revision'],'error_code':subject['error_code'],'diagnosis':diagnosis,
                'repair_policy':resolution.action})
        elif resolution.action=='retry_same_revision':
            store.execute("UPDATE workflow_tasks SET status='retry_wait',next_run_at=?,wait_reason='deterministic_recovery',error_code='',error_detail='' WHERE workflow_id=? AND id=?",
                          (store.now(),run['id'],subject['id']))
            self.set_status(run,'running','deterministic_recovery')
        else:self.set_status(run,'paused',resolution.action)
        graph.event(store,run,'diagnosis_completed',{'task_id':rows[0]['subject_task'],'request_id':rows[0]['id']})

    def accept_review(self,run,task,data):
        store=self.store
        rows=store.query('SELECT * FROM workflow_reviews WHERE id=? AND workflow_id=?',(task['spec']['request_id'],run['id']))
        if not rows:raise ValueError('Unknown review')
        from .workflow_agent_contracts import ResolvedReviewDecision
        row=rows[0];decision=ResolvedReviewDecision.model_validate(data.get('decision',{})).model_dump()
        if decision.get('snapshot_digest')!=row['snapshot_digest']:raise ValueError('Review snapshot mismatch')
        if row['purpose']=='final' and (row['graph_version']!=run['graph_version'] or not run['sealed']):raise ValueError('Final review graph changed')
        if row['subject_task']:
            frozen=json.loads(row['snapshot_json'])
            prior=next(t for t in frozen['tasks'] if t['id']==row['subject_task'])
            current=next(t for t in run['tasks'] if t['id']==row['subject_task'])
            if any(prior[key]!=current[key] for key in ('spec','revision','candidate_result','quality')):
                raise ValueError('Reviewed task inputs or candidate changed')
        evidence=decision.get('evidence',[])
        passed=decision.get('verdict')=='pass' and bool(evidence) and all(isinstance(x,dict) and x.get('criterion') and x.get('observation') for x in evidence)
        if passed:
            inspected={r['reference'] for r in store.query('SELECT reference FROM workflow_inspections WHERE workflow_id=? AND task_id=? AND attempt_id=? AND snapshot_digest=?',
                        (run['id'],task['id'],task['lease_id'],row['snapshot_digest']))}
            if any(item.get('ref') not in inspected for item in evidence):passed=False
            if row['purpose']=='final' and not set(run['authorization']['delivery_targets'])<={item['criterion'] for item in evidence}:passed=False
            if row['purpose']=='final' and not set(decision['deliverables'])<=inspected:passed=False
        store.execute('UPDATE workflow_reviews SET status=?,decision_json=?,updated_at=? WHERE id=?',('passed' if passed else 'rejected',encode(decision),store.now(),row['id']))
        store.execute("UPDATE workflow_tasks SET status='succeeded' WHERE workflow_id=? AND id=?",(run['id'],task['id']))
        if passed and row['subject_task']:
            subject=next(t for t in graph.get(store,run['id'])['tasks'] if t['id']==row['subject_task'])
            if not subject['quality'].get('technical_valid'):raise ValueError('Review cannot override technical failure')
            quality={**subject['quality'],'agent_review_passed':True}
            store.execute('UPDATE workflow_tasks SET quality_json=? WHERE workflow_id=? AND id=?',(encode(quality),run['id'],subject['id']))
            if quality.get('requires_review'):
                self.decision(run,'quality',{'task_id':subject['id'],'revision':subject['revision'],'quality':quality})
            else:store.execute("UPDATE workflow_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE workflow_id=? AND id=?",(run['id'],subject['id']))
        elif not passed:
            if row['purpose']=='final':
                self.open_repair(run,'','final_review',row['id'],'quality_failed')
                store.execute('UPDATE workflow_runs SET sealed=0 WHERE id=?',(run['id'],))
                self.set_status(run,'planning','final_review_rejected')
                planning.queue(store,graph.get(store,run['id']),{'review_rejected':row['id'],'decision':decision})
            else:
                store.execute("UPDATE workflow_tasks SET status='paused',error_code='quality_failed',error_detail=? WHERE workflow_id=? AND id=?",
                    ('内容验收未通过：'+(decision.get('feedback') or '请检查候选并重新规划。')[:1600],run['id'],row['subject_task']))
                self.open_repair(run,row['subject_task'],'task_review',row['id'],'quality_failed')
                store.execute('UPDATE workflow_runs SET sealed=0 WHERE id=?',(run['id'],))
                self.set_status(run,'planning','task_review_rejected')
                planning.queue(store,graph.get(store,run['id']),{'task_review_rejected':row['id'],
                    'task_id':row['subject_task'],'decision':decision,'repair_policy':'manager_revision'})

    def publish(self,run,review):
        store=self.store
        if run['delivered_at'] is not None:return
        if store.query("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending'",(run['id'],)):
            self.set_status(run,'waiting_user','user_decision_pending');return
        decision=json.loads(review['decision_json']);selected=decision.get('deliverables',[])
        accepted={name:t for t in run['tasks'] if not t['internal'] for name in t['accepted_result']}
        if not selected or not isinstance(selected,list) or any(name not in accepted for name in selected):
            self.set_status(run,'paused','invalid_delivery_manifest');return
        primary=decision.get('primary') or (selected[0] if len(set(selected))==1 else '')
        if primary not in selected:
            self.set_status(run,'paused','invalid_delivery_manifest');return
        artifacts=[];publications=[]
        import mimetypes
        from pathlib import PurePosixPath
        for name in dict.fromkeys(selected):
            task=accepted[name]
            rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                             (run['id'],task['id'],task['revision']))
            record=json.loads(rows[0]['manifest']).get(name) if rows else None
            if not record:raise ValueError('Delivery lacks a durable checkpoint')
            filename=task['id']+'-'+record['artifact'][:12]+'-'+PurePosixPath(name).name
            logical='edit/agent_exports/'+run['turn_id']+'/'+filename
            if not store.shared_files:raise ValueError('Dynamic delivery requires shared immutable artifact storage')
            obj=store.artifacts.record(run['project_id'],record['artifact'])
            if obj['sha256']!=record['sha256']:raise ValueError('Delivery changed after review')
            publications.append((logical,record['artifact']))
            artifacts.append({'file':filename,'name':PurePosixPath(name).name,'size':record['size'],'sha256':record['sha256'],
                'type':mimetypes.guess_type(filename)[0] or 'application/octet-stream','role':'deliverable',
                'quality_state':'approved','workflow_delivery':True,'source_step':task['id'],'source_revision':task['revision'],
                'is_primary':name==primary})
        # The explicit, reviewed immutable reference owns primary selection.
        # Shared ordering only arranges the remaining approved deliverables.
        from .artifacts import artifact_key
        # Keep lossless PNG before equivalent JPEG among non-primary attachments.
        artifacts.sort(key=lambda item:(artifact_key(item)[:-1],
            PurePosixPath(item['name']).suffix.lower()!='.png',artifact_key(item)[-1]))
        try:store.check_storage(run['project_id'],sum(a['size'] for a in artifacts),replace_turn=run['turn_id'])
        except ValueError:
            self.set_status(run,'paused','storage_quota_exceeded');return
        for logical,artifact in publications:store.artifacts.publish(run['project_id'],logical,artifact)
        if store.execute("UPDATE workflow_runs SET delivered_at=?,delivery_json=?,status='succeeded',updated_at=? WHERE id=? AND delivered_at IS NULL AND sealed=1",
                         (store.now(),encode(artifacts),store.now(),run['id'])):
            store.execute("UPDATE agent_turns SET status='succeeded',artifacts=?,text=?,updated=? WHERE id=?",(encode(artifacts),'工作流已完成并通过最终验收。',store.now(),run['turn_id']))
            graph.event(store,run,'delivered',{'files':[a['file'] for a in artifacts]})

    def cancel(self,run):
        store=self.store
        from .task_lifecycle import request_cleanup
        with store.connect() as db:request_cleanup(db,run['turn_id'])
        store.execute("UPDATE workflow_tasks SET status='cancelled' WHERE workflow_id=? AND status NOT IN ('succeeded','failed','cancelled')",(run['id'],))
        store.execute("UPDATE planner_runs SET status='rejected',error_code='cancelled' WHERE workflow_id=? AND status IN ('pending','running','staged')",(run['id'],))
        store.execute("UPDATE workflow_reviews SET status='cancelled' WHERE workflow_id=? AND status IN ('pending','running','staged')",(run['id'],))
        store.execute("UPDATE workflow_decisions SET status='cancelled' WHERE workflow_id=? AND status='pending'",(run['id'],))
        busy=store.query("SELECT attempt FROM execution_leases WHERE turn_id=? AND active=1 AND expires>?",(run['turn_id'],store.now()))
        cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['turn_id'],))
        if not busy and not cleanup:
            terminal='failed' if run['terminal_reason']=='deadline_or_budget' else 'cancelled'
            self.set_status(run,terminal,run['terminal_reason'])
            store.execute('UPDATE agent_turns SET status=?,updated=? WHERE id=?',(terminal,store.now(),run['turn_id']))

    def run(self):
        from .telemetry import emit,release,bind
        import time
        while not self.stop.is_set():
            try:
                self.store.execute('''INSERT INTO workflow_orchestrators(id,release,heartbeat) VALUES (?,?,?)
                    ON CONFLICT(id) DO UPDATE SET release=excluded.release,heartbeat=excluded.heartbeat,draining=0''',
                    (self.owner,release(),self.store.now()))
                for identifier,generation in self.claim_due():
                    if self.stop.is_set():break
                    started=time.monotonic();failed=False;run=None
                    try:
                        run=graph.get(self.store,identifier)
                        with bind(task_id=run['turn_id'],workflow_id=identifier,reconcile_generation=generation):
                            self.reconcile(identifier,generation)
                            emit('workflow.reconciled',duration_seconds=time.monotonic()-started,graph_version=run['graph_version'])
                    except ReconcileLost:pass
                    except Exception as exc:
                        failed=True;code=getattr(exc,'sqlstate','') or getattr(exc,'code','') or ''
                        try:self.store.execute('INSERT INTO workflow_reconcile_failures VALUES (?,?,?,?,?,?)',
                            (uuid.uuid4().hex,identifier,generation,type(exc).__name__,str(code)[:120],self.store.now()))
                        except Exception as record_error:emit('workflow.reconcile_failure_record_failed','ERROR',exc=record_error,workflow_id=identifier)
                        emit('workflow.reconcile_failed','ERROR',exc=exc,workflow_id=identifier,task_id=run['turn_id'] if run else None)
                    self.store.execute('UPDATE workflow_orchestrators SET reconciles=reconciles+1,failures=failures+?,last_duration=? WHERE id=?',(int(failed),time.monotonic()-started,self.owner))
            except Exception as exc:emit('workflow.scan_failed','ERROR',exc=exc)
            self.stop.wait(1)
        try:self.store.execute('UPDATE workflow_orchestrators SET draining=1,heartbeat=? WHERE id=?',(self.store.now(),self.owner))
        except Exception as exc:emit('workflow.drain_record_failed','WARNING',exc=exc)


def re_full_handle(value):
    import re
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value)


def main():
    from .store import Store
    from .paths import data_root
    from .telemetry import setup
    setup('orchestrator');store=Store(data_root(),initialize_schema=False)
    service=Orchestrator(store)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:service.stop.set())
    service.run()


if __name__=='__main__':main()
