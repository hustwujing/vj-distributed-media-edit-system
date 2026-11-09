"""Deterministic workflow controller. No model or media execution in this process."""
from __future__ import annotations
import json
import os
import signal
import threading
import uuid
from . import workflow_graph as graph,workflow_planning as planning
from .workflow_graph import encode,digest,TERMINAL,RUN_TERMINAL,AuthorizationRequired


class ReconcileLost(RuntimeError):pass
class InvalidProviderHandle(ValueError):pass
class DecisionError(ValueError):
    def __init__(self,code,message=''):
        super().__init__(message or code);self.code=code


def delivery_scope(store,run):
    """Extract output requirements without treating authorized input filenames as outputs."""
    asset_ids=run['authorization'].get('allowed_assets',[])
    input_names=[]
    if asset_ids:
        placeholders=','.join('?' for _ in asset_ids)
        for asset in store.query(f'SELECT name,filename FROM assets WHERE id IN ({placeholders})',tuple(asset_ids)):
            for value in (asset.get('name'),asset.get('filename')):
                if value:input_names.append(value)
    changes=store.query("SELECT content FROM run_inputs WHERE run_id=? AND status='APPLIED' ORDER BY sequence",(run['id'],))
    from .effective_delivery import confirmed_scope
    return {**run,'input_asset_names':input_names,'user_changes':changes,'effective_delivery_scope':confirmed_scope(store,run)}


class Orchestrator:
    def __init__(self,store,owner=None,ttl=15):
        self.store=store;self.owner=owner or uuid.uuid4().hex;self.ttl=ttl
        self.stop=threading.Event()

    def claim_due(self,limit=100):
        limit=max(1,min(100,int(limit)))
        with self.store.connect() as db:
            now=self.store.now()
            sql="""SELECT id,reconcile_generation FROM runs WHERE (status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
                OR EXISTS(SELECT 1 FROM run_inputs i WHERE i.run_id=runs.id AND i.status='ROUTING'))
                AND next_reconcile_at<=? AND reconcile_until<=? ORDER BY next_reconcile_at,id LIMIT ?"""
            if self.store.dsn:sql+=' FOR UPDATE SKIP LOCKED'
            rows=db.execute(sql,(now,now,limit)).fetchall();claims=[]
            for row in rows:
                generation=row['reconcile_generation']+1
                if db.execute('''UPDATE runs SET reconcile_owner=?,reconcile_until=?,reconcile_generation=?
                    WHERE id=? AND reconcile_until<=? AND reconcile_generation=?''',
                    (self.owner,now+self.ttl,generation,row['id'],now,row['reconcile_generation'])).rowcount:
                    claims.append((row['id'],generation))
            return claims

    def check(self,db,identifier,generation):
        row=db.execute('SELECT reconcile_owner,reconcile_until,reconcile_generation FROM runs WHERE id=?',(identifier,)).fetchone()
        if not row or row['reconcile_owner']!=self.owner or row['reconcile_generation']!=generation or row['reconcile_until']<=self.store.now():
            raise ReconcileLost('Workflow reconcile lease expired or replaced')

    def reconcile(self,identifier,generation):
        store=self.store
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.check(db,identifier,generation)
            run=graph.get(store,identifier)
            from . import workflow_messages as messages
            if run['status'] in RUN_TERMINAL:
                if run['status']=='SUCCEEDED':
                    self.reconcile_model_reservations(run)
                    self.accept_results(run)
                    messages.recover(store,graph.get(store,identifier))
                    messages.queue(store,graph.get(store,identifier))
                else:
                    for message in messages.pending(store,run['id']):
                        messages.fail(store,run,{'id':'message_'+message['id'],'spec':{'message_input_id':message['id']}},'task_stopped_before_message_reply')
                db.execute("UPDATE runs SET next_reconcile_at=?,reconcile_until=0,reconcile_owner='' WHERE id=? AND reconcile_owner=? AND reconcile_generation=?",(store.now()+1,identifier,self.owner,generation))
                return
            if (run['status']=='WAITING_USER' and run['terminal_reason']=='delivery_targets_incomplete'
                and store.query("SELECT id FROM run_inputs WHERE run_id=? AND status='APPLIED' LIMIT 1",(run['id'],))
                and not store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))):
                self.decision(run,'delivery_scope_change',{
                    'question':'当前成果未满足全部交付目标。请确认新的交付范围；已有合格成果可以复用。',
                    'current_targets':run['authorization'].get('delivery_targets',[]),
                    'reason':'已有后续输入，但当前成果仍未覆盖已确认的交付范围。请核对是否需要变更范围；保留原范围时应补齐缺少的交付。'})
            self.commands(run)
            run=graph.get(store,identifier)
            user_paused=run['status']=='WAITING_USER' and run['terminal_reason']=='user_paused'
            self.accept_results(run)
            run=graph.get(store,identifier)
            messages.recover(store,run)
            messages.queue(store,graph.get(store,identifier))
            run=graph.get(store,identifier)
            self.recover(run)
            run=graph.get(store,identifier)
            if user_paused and run['status'] not in ('CANCELLING',*RUN_TERMINAL):
                self.set_status(run,'WAITING_USER','user_paused');run=graph.get(store,identifier)
            # Process commands and any staged completion first. A timeout must
            # neither starve its own answer/cancel command nor mask a result.
            wait_budget=float(os.environ.get('VIDEO_USE_EXTERNAL_WAIT_SECONDS','7200'))
            timed_out=[task for task in graph.active_tasks(run) if task['status']=='waiting'
                       and task['phase'] in ('poll','collect') and task.get('wait_recorded_at')
                       and store.now()-task['wait_recorded_at']>wait_budget]
            if run['status']=='ACTIVE' and timed_out:
                self.decision(run,'external_wait',{'task_id':timed_out[0]['id'],'question':'外部任务等待已超时。请选择继续等待，或放弃该任务并保留其他成果后重新规划。','options':['keep_waiting','abandon_task']})
                self.set_status(run,'WAITING_USER','external_wait_timeout')
                run=graph.get(store,identifier)
            if run['input_barrier_sequence'] is not None and run['status'] not in ('CANCELLING',*RUN_TERMINAL):
                self.apply_planner(run)
                self.advance_ready(graph.get(store,identifier))
                self.process_inputs(graph.get(store,identifier))
                run=graph.get(store,identifier)
            if run['status']=='CANCELLING':self.cancel(run)
            elif run['status'] not in ('WAITING_USER',*RUN_TERMINAL):
                self.apply_planner(run)
                run=graph.get(store,identifier)
                if run['status'] not in ('WAITING_USER',*RUN_TERMINAL):
                    self.advance(run)
            self.check(db,identifier,generation)
            retry_at=store.now()+1
            db.execute('''UPDATE runs SET next_reconcile_at=CASE WHEN next_reconcile_at>? THEN next_reconcile_at ELSE ? END,reconcile_until=0,reconcile_owner='',updated_at=?
                WHERE id=? AND reconcile_owner=? AND reconcile_generation=?''',
                (retry_at,retry_at,store.now(),identifier,self.owner,generation))

    def process_inputs(self,run):
        from .run_inputs import safe_point_reason,freeze_batch,InputAttachmentChanged
        if run['terminal_reason'] in ('user_paused','user_rejected','clarification_exhausted'):return
        reason=safe_point_reason(self.store,run)
        if reason:
            if reason in ('input_waiting_external_unknown','input_waiting_user_decision'):
                self.set_status(run,'WAITING_USER',reason)
            return
        with graph.atomic_effect(self.store):
            try:batch=freeze_batch(self.store,run)
            except InputAttachmentChanged as exc:
                self.decision(run,'input_question',{'question':'关联素材已变化或被删除，请重新选择素材并补充说明。', 'invalid_input_ids':exc.input_ids})
                return
            if not batch:return
            batch['was_sealed']=bool(run['input_barrier_was_sealed'])
            self.store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
            self.set_status(run,'ACTIVE','input_replanning')
            current=graph.get(self.store,run['id'])
            try:queued=planning.queue(self.store,current,{'input_batch':batch})
            except planning.PlanningBudgetExceeded:
                from .run_inputs import defer_batch
                defer_batch(self.store,current,batch['through_sequence'])
                self.set_status(current,'WAITING_USER','planning_budget_exhausted')
                return
            if not queued:
                from .run_inputs import defer_batch
                defer_batch(self.store,current,batch['through_sequence'])
                return
            graph.event(self.store,current,'input_safe_point',{'through_sequence':batch['through_sequence']})

    def queue_plan(self,run,trigger):
        # Failures/diagnoses may finish while other drain-set work is running.
        # Preserve their committed facts and let the inbox safe point create the
        # single PLAN containing both those facts and the frozen input batch.
        if graph.get(self.store,run['id'])['input_barrier_sequence'] is not None:return None
        try:return planning.queue(self.store,run,trigger)
        except planning.PlanningBudgetExceeded:
            self.set_status(run,'WAITING_USER','planning_budget_exhausted')
            return None

    def set_status(self,run,status,reason=''):
        self.store.execute('UPDATE runs SET status=?,terminal_reason=?,updated_at=? WHERE id=?',
                           (status,reason,self.store.now(),run['id']))
        if run['status']!=status:graph.event(self.store,run,'state_changed',{'status':status,'reason':reason})

    def begin_failure(self,run,reason):
        self.set_status(run,'CANCELLING',reason)
        from .task_lifecycle import request_cleanup
        with self.store.connect() as db:request_cleanup(db,run['id'])

    def decision(self,run,kind,request):
        store=self.store
        if kind in ('quality','quality_feedback'):
            from .run_inputs import candidate_digest
            current=graph.get(store,run['id'])
            subject=next(t for t in current['tasks'] if t['id']==request['task_id'])
            request={**request,'candidate_digest':candidate_digest(store,current,subject)}
        current=store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND kind=? AND status='pending'",(run['id'],kind))
        if not current:
            frozen={'graph_version':run['graph_version'],'authorization_version':run['authorization_version'],'request':request}
            store.execute('''INSERT INTO workflow_decisions(id,run_id,kind,graph_version,authorization_version,
                snapshot_digest,request_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)''',
                (uuid.uuid4().hex,run['id'],kind,run['graph_version'],run['authorization_version'],digest(frozen),encode(request),store.now(),store.now()))
        self.set_status(run,'WAITING_USER',kind)

    def open_repair(self,run,subject,source,evidence='',error_code=''):
        context={'active':True,'subject_task':subject,'subject_revision':next((t['revision'] for t in graph.work_items(run) if t['id']==subject),0) if subject else None,
                 'source':source,'evidence':evidence,'error_code':error_code,'opened_graph_version':run['graph_version']}
        self.store.execute('UPDATE runs SET repair_context_json=?,updated_at=? WHERE id=?',
                           (encode(context),self.store.now(),run['id']))
        graph.event(self.store,run,'repair_context_opened',{'task_id':subject,'error_code':error_code,'source':source})
        return context

    def commands(self,run):
        store=self.store
        for command in store.query("SELECT * FROM workflow_commands WHERE run_id=? AND status='pending' ORDER BY created_at,id LIMIT 100",(run['id'],)):
            run=graph.get(store,run['id']);payload=json.loads(command['payload_json']);error=''
            if run['status'] in RUN_TERMINAL:error='workflow_terminal'
            elif run['status']=='CANCELLING' and command['action']!='cancel':error='cancellation_in_progress'
            elif command['action']=='cancel':
                self.set_status(run,'CANCELLING','user_cancelled')
                from .task_lifecycle import request_cleanup
                with store.connect() as db:request_cleanup(db,run['id'])
            elif command['action']=='pause':self.set_status(run,'WAITING_USER','user_paused')
            elif command['action']=='resume':
                unresolved=store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))
                requires_replan={'planning_budget_exhausted','model_token_budget_exhausted','budget_exhausted','daily_model_quota_exhausted',
                    'invalid_delivery_manifest','delivery_targets_incomplete','diagnostic_policy_not_authorized',
                    'user_decision','new_run_rejected','clarification_exhausted','user_rejected'}
                if run['status']!='WAITING_USER':error='resume_requires_safe_paused_state'
                elif unresolved and run['terminal_reason']=='user_paused':self.set_status(run,'WAITING_USER','user_decision_pending')
                elif unresolved:error='decision_requires_answer'
                elif run['terminal_reason']=='execution_failed':
                    failed=[task for task in graph.work_items(run) if task['status']=='paused' and task.get('error_code') in ('execution_failed','agent_failed')]
                    def environment_failure(task):
                        records=store.query('SELECT record FROM workflow_diagnostics WHERE workflow=? AND step=? ORDER BY created DESC LIMIT 1',(run['id'],task['id']))
                        if not records:return False
                        try:
                            record=json.loads(records[0]['record'])
                            return (record.get('classification') in ('environment','resource','network','timeout','execution') and
                                    record.get('error_type') not in ('PlanError','ValueError','ValidationError','ModelResultError'))
                        except (TypeError,ValueError):return False
                    retryable=bool(failed) and all(environment_failure(task) for task in failed)
                    if retryable:
                        for task in failed:
                            store.execute(f"UPDATE {graph.work_table(task)} SET status='ready',phase='local',lease_id='',lease_generation=0,error_code='',error_detail='',wait_reason='',next_run_at=? WHERE run_id=? AND id=?",
                                          (store.now(),run['id'],task['id']))
                        self.set_status(run,'ACTIVE','execution_retry')
                    else:error='replan_required'
                elif run['terminal_reason'] in requires_replan:error='replan_required'
                elif any(t.get('control_kind') and t['spec'].get('profile')=='media_failure_diagnostician' and t['status']=='ready' for t in graph.work_items(run)):
                    self.set_status(run,'ACTIVE')
                elif any(t['error_code'] in ('submission_unknown','result_unavailable') for t in graph.work_items(run)):error='external_result_requires_resolution'
                elif any(t['error_code'] in ('model_result','quality_failed','review_incomplete','user_revision_requested','invalid_result_manifest','pre_submit') for t in graph.work_items(run)):error='replan_required'
                elif any(t['status']=='paused' and not t.get('control_kind') and t['phase'] in ('local','sync','submit')
                         and (t['spec'].get('retry_policy')!='safe_once' or t['attempts']>=2) for t in graph.work_items(run)):error='replan_required'
                else:
                    for task in graph.work_items(run):
                        if task['status']=='paused' and (task['phase'] in ('poll','collect','validate') or not task.get('control_kind') and task['spec'].get('retry_policy')=='safe_once'):
                            store.execute("UPDATE run_tasks SET status='retry_wait',next_run_at=?,wait_reason='cleanup',error_code='',error_detail='',recovery_count=0 WHERE run_id=? AND id=?",(store.now(),run['id'],task['id']))
                    self.set_status(run,'ACTIVE')
            elif command['action']=='answer':
                try:self.answer(run,payload,request_id=command['id'],actor=command['actor'])
                except DecisionError as exc:error=exc.code
                except (ValueError,KeyError,TypeError):error='invalid_or_stale_decision'
            store.execute('UPDATE workflow_commands SET status=?,error_code=?,applied_at=? WHERE id=?',
                          ('rejected' if error else 'applied',error,store.now(),command['id']))
            graph.event(store,run,'command_processed',{'command_id':command['id'],'action':command['action'],'error_code':error})

    def answer(self,run,payload,*,request_id=None,actor='user'):
        with graph.atomic_effect(self.store):
            return self._answer(graph.get(self.store,run['id']),payload,request_id=request_id,actor=actor)

    def external_submission_subject(self,run,request):
        task=graph.work_item(run,request['task_id'])
        if not task or task['type']!='operation' or task['status']!='paused' or task['error_code']!='submission_unknown':
            raise DecisionError('external_submission_changed')
        key=f'{run["id"]}:{task["id"]}:{task["revision"]}'
        rows=self.store.query('SELECT * FROM workflow_provider_calls WHERE request_key=?',(key,))
        if not rows or rows[0]['state'] not in ('submitting','unknown','submission_unknown'):
            raise DecisionError('external_submission_changed')
        if self.store.query('SELECT l.attempt FROM execution_leases l JOIN execution_attempts a ON a.id=l.attempt WHERE l.run_id=? AND a.step_id=? AND l.active=1 AND l.expires>?',
                            (run['id'],task['id'],self.store.now())):
            raise DecisionError('external_recovery_attempt_active')
        if self.store.query("SELECT c.attempt FROM execution_cleanup c JOIN execution_attempts a ON a.id=c.attempt WHERE a.run_id=? AND a.step_id=? AND c.state<>'complete'",
                            (run['id'],task['id'])):
            raise DecisionError('external_recovery_cleanup_pending')
        return task,rows[0]

    def _answer(self,run,payload,*,request_id=None,actor='user'):
        store=self.store
        payload=graph.DecisionAnswer.model_validate(payload).model_dump(exclude_none=True)
        rows=store.query("SELECT * FROM workflow_decisions WHERE id=? AND run_id=? AND status='pending'",(payload.get('decision_id'),run['id']))
        if not rows or run['status']!='WAITING_USER':raise DecisionError('decision_not_pending')
        row=rows[0]
        if row['graph_version']!=run['graph_version'] or row['authorization_version']!=run['authorization_version'] or payload.get('snapshot_digest')!=row['snapshot_digest']:
            raise DecisionError('decision_snapshot_stale')
        request=json.loads(row['request_json'])
        if row['kind']=='plan_confirmation' and payload['action']=='approve':
            if run['input_sequence']!=request['input_sequence']:raise DecisionError('decision_snapshot_stale')
            if graph.content_fingerprint(store,run['project_id'])!=request['content_fingerprint']:raise DecisionError('content_changed')
            plans=store.query("SELECT * FROM planner_runs WHERE id=? AND run_id=? AND status='awaiting_confirmation'",
                              (request['planning_request_id'],run['id']))
            if not plans or graph.digest(json.loads(plans[0]['proposal_json']))!=request['proposal_digest']:
                raise DecisionError('decision_snapshot_stale')
            plan=plans[0]
            store.execute("UPDATE planner_runs SET status='staged' WHERE id=?",(plan['id'],))
            self.set_status(run,'ACTIVE','plan_confirmed')
            planning.apply(store,graph.get(store,run['id']),{**plan,'status':'staged'},confirmed_plan=True)
            store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                          (encode(payload),store.now(),row['id']))
            return
        if row['kind'] in ('quality','quality_feedback'):
            from .run_inputs import candidate_digest
            subject=next(t for t in run['tasks'] if t['id']==request['task_id'])
            if subject['revision']!=request['revision'] or request['candidate_digest']!=candidate_digest(store,run,subject):
                raise ValueError('Candidate changed after the decision snapshot')
        if payload.get('action')=='revise':
            from .run_inputs import accept,revision_closure
            if row['kind'] not in ('quality','quality_feedback','input_question','plan_confirmation'):raise ValueError('Decision does not support feedback')
            if not request_id:raise ValueError('Feedback requires an idempotency request ID')
            context={'decision_id':row['id'],'decision_snapshot':row['snapshot_digest']}
            if row['kind']=='plan_confirmation':
                from .workflow_conversation import invalidate_plan
                context['superseded_plan']={key:request[key] for key in ('reason','delivery_targets','steps','dependencies')}
                invalidate_plan(store,run,row)
            if row['kind'] not in ('input_question','plan_confirmation'):
                if subject['user_revision_count']>=5:raise ValueError('User revision limit reached')
                context.update(subject_task=subject['id'],subject_revision=subject['revision'],
                    candidate_digest=request['candidate_digest'],candidate=subject['candidate_result'],quality=subject['quality'],
                    allowed_revision_tasks=revision_closure(run,subject['id'],payload['upstream_tasks']))
                context['review_evidence']=[json.loads(item['decision_json']) for item in store.query(
                    "SELECT decision_json FROM workflow_reviews WHERE run_id=? AND subject_task=? ORDER BY created_at DESC LIMIT 5",(run['id'],subject['id']))]
                store.execute("UPDATE run_tasks SET status='paused',error_code='user_revision_requested' WHERE run_id=? AND id=?",(run['id'],subject['id']))
            invalid_inputs=request.get('invalid_input_ids',[]) if row['kind']=='input_question' else []
            if invalid_inputs:
                context['superseded_inputs']=[{'id':item['id'],'content':item['content'],'context':json.loads(item['context_json'])} for item in store.query("SELECT id,content,context_json FROM run_inputs WHERE run_id=? AND status='ACCEPTED'",(run['id'],)) if item['id'] in invalid_inputs]
            accept(store,run['id'],actor,{'request_id':request_id,'content':payload['feedback'],
                'attachments':payload['attachments'],'source':'user' if row['kind'] in ('input_question','plan_confirmation') else 'quality_feedback'},context=context)
            if invalid_inputs:
                from .run_inputs import supersede_inputs
                supersede_inputs(store,run,invalid_inputs,'attachment_replaced_by_user')
            self.set_status(run,'ACTIVE','input_draining')
        elif payload.get('action')=='reject':
            if row['kind']=='plan_confirmation':
                from .workflow_conversation import invalidate_plan
                from .run_inputs import reject_batch
                invalidate_plan(store,run,row)
                reject_batch(store,run,int(request.get('input_through_sequence') or 0),'user_rejected_plan')
                store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
                self.set_status(run,'WAITING_USER','user_rejected')
                return
            if row['kind']=='external_wait':
                task=next(t for t in graph.work_items(run) if t['id']==request['task_id']);graph.write_work(store,task,"UPDATE run_tasks SET status='paused',error_code='external_wait_timeout',error_detail=? WHERE run_id=? AND id=?",('外部任务等待超时，已放弃等待。',run['id'],task['id']));store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
                from .task_lifecycle import request_cleanup
                with store.connect() as db:request_cleanup(db,run['id'],external=True,step_id=task['id'])
                self.open_repair(run,task['id'],'external_wait',row['id'],'external_wait_timeout');store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],));self.set_status(run,'ACTIVE','external_wait_replan');self.queue_plan(graph.get(store,run['id']),{'external_wait_abandoned':row['id'],'task_id':task['id']});return
            if row['kind']=='external_submission':
                task,call=self.external_submission_subject(run,request)
                if call['external_id']:raise DecisionError('external_submission_has_known_id')
                from .operation_lifecycle import for_operation
                from .operations import get as operation
                phase=for_operation(operation(task['spec']['operation'],task['spec']['implementation'])).protocol.state_phases['pending']
                import uuid
                store.execute("UPDATE workflow_provider_calls SET request_key=request_key||?,state='superseded',updated=? WHERE request_key=?",(':superseded:'+uuid.uuid4().hex,store.now(),call['request_key']))
                graph.write_work(store,task,"UPDATE run_tasks SET status='retry_wait',phase=?,external_id='',error_code='',error_detail='',next_run_at=? WHERE run_id=? AND id=?",(phase,store.now(),run['id'],task['id']))
                store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
                self.set_status(run,'ACTIVE','external_submission_resubmit');return
            if row['kind'] in ('plan_change','successor_authorization'):
                cutoff=int(request.get('input_through_sequence') or 0)
                if cutoff:
                    from .run_inputs import reject_batch
                    reject_batch(store,run,cutoff)
                    self.set_status(run,'ACTIVE','authorization_rejected')
                else:self.set_status(run,'FAILED','authorization_rejected')
            elif row['kind']=='input_question':
                from .run_inputs import reject_batch,supersede_inputs
                cutoff=int(request.get('input_through_sequence') or 0)
                invalid_inputs=request.get('invalid_input_ids',[])
                if cutoff:reject_batch(store,run,cutoff,'user_rejected_input')
                elif invalid_inputs:supersede_inputs(store,run,invalid_inputs,'user_rejected_input')
                remaining=store.query("SELECT id FROM run_inputs WHERE run_id=? AND sequence>? AND status='ACCEPTED' LIMIT 1",(run['id'],cutoff))
                continue_work=bool(remaining or run['tasks'])
                self.set_status(run,'ACTIVE' if continue_work else 'WAITING_USER',
                                'input_draining' if continue_work else 'user_rejected')
            elif row['kind']=='new_run':self.set_status(run,'WAITING_USER','new_run_rejected')
            if row['kind'] in ('quality','quality_feedback'):
                subject=json.loads(row['request_json'])['task_id']
                store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed',error_detail=? WHERE run_id=? AND id=?",
                    ('用户未通过内容验收，请调整要求后重新规划。',run['id'],subject))
                self.set_status(run,'WAITING_USER','user_rejected')
        elif row['kind']=='external_wait' and payload.get('action')=='approve':
            task=next(t for t in graph.work_items(run) if t['id']==request['task_id']);store.execute("UPDATE run_tasks SET wait_recorded_at=?,next_run_at=? WHERE run_id=? AND id=?",(store.now(),store.now(),run['id'],task['id']));store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']));self.set_status(run,'ACTIVE','external_wait_extended');return
        elif row['kind']=='external_submission' and payload.get('action')=='approve':
            external_id=str(payload.get('external_id') or '').strip()
            if not re_full_handle(external_id):raise ValueError('请填写供应商任务 ID 后再继续')
            task,call=self.external_submission_subject(run,request)
            from .operation_lifecycle import for_operation
            from .operations import get as operation
            if 'poll' not in for_operation(operation(task['spec']['operation'],task['spec']['implementation'])).protocol.phases:
                raise DecisionError('external_submission_not_queryable')
            if call['external_id'] and call['external_id']!=external_id:raise DecisionError('external_submission_id_conflict')
            store.execute("UPDATE workflow_provider_calls SET state='waiting',external_id=?,updated=? WHERE request_key=?",
                          (external_id,store.now(),call['request_key']))
            graph.write_work(store,task,"UPDATE run_tasks SET status='waiting',phase='poll',external_id=?,error_code='',error_detail='',wait_recorded_at=?,next_run_at=? WHERE run_id=? AND id=?",(external_id,store.now(),store.now(),run['id'],task['id']))
            store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
            self.set_status(run,'ACTIVE','external_submission_attached');return
        elif row['kind']=='new_run' and payload.get('action')=='approve':
            from .run_inputs import accept
            if not payload.get('authorization'):raise ValueError('Successor requires explicit scope approval')
            authorization=planning.rebind_scope(store,payload['authorization'])
            if authorization.goal!=request['new_goal']:raise ValueError('Successor goal changed after the decision')
            if store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],store.now())) or store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],)):
                raise ValueError('Successor must wait for the original Run to drain')
            if store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],)):
                raise ValueError('Successor cannot abandon an unresolved external call')
            self.set_status(run,'CANCELLED','successor_confirmed')
            store.execute("UPDATE run_tasks SET status='cancelled' WHERE run_id=? AND selected=1 AND status NOT IN ('succeeded','failed','cancelled')",(run['id'],))
            successor=graph.create(store,run['project_id'],None,{**authorization.model_dump(),'require_plan_confirmation':True},parent_run_id=run['id'])
            cutoff=int(request.get('input_through_sequence',0))
            for item in store.query("SELECT * FROM run_inputs WHERE run_id=? AND status IN ('ROUTING','ACCEPTED','PLANNING') ORDER BY sequence",(run['id'],)):
                carried=accept(store,successor['id'],actor,{'request_id':item['id'],'content':item['content'],
                    'attachments':[a['id'] for a in json.loads(item['attachments_json'])]},context={'predecessor_input_id':item['id'],
                        **({'message_disposition':'replan'} if item['status']!='ROUTING' else {})})
                if item['status']!='ROUTING' and item['sequence']<=cutoff:
                    now=store.now()
                    store.execute("UPDATE run_inputs SET status='APPLIED',applied_at=?,applied_graph_version=? WHERE id=?",
                                  (now,successor['graph_version'],carried['id']))
                    graph.event(store,successor,'successor_input_applied',{
                        'input_id':carried['id'],'predecessor_input_id':item['id'],'input_sequence':carried['sequence']})
                store.execute("UPDATE run_inputs SET status='SUPERSEDED',successor_run_id=?,applied_at=? WHERE id=?",(successor['id'],store.now(),item['id']))
            from .run_inputs import finish_batch
            finish_batch(store,graph.get(store,successor['id']),cutoff)
            store.execute('UPDATE runs SET input_barrier_sequence=NULL WHERE id=?',(run['id'],))
            store.execute("UPDATE control_jobs SET status='cancelled',updated_at=? WHERE run_id=? AND control_kind='MESSAGE' AND status NOT IN ('succeeded','failed','cancelled')",(store.now(),run['id']))
            store.execute("UPDATE workflow_decisions SET status='superseded' WHERE run_id=? AND status='pending' AND id<>?",(run['id'],row['id']))
            store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode({**payload,'successor_run_id':successor['id']}),store.now(),row['id']))
            graph.event(store,run,'successor_created',{'successor_run_id':successor['id']})
            return
        elif row['kind'] in ('plan_change','successor_authorization') and payload.get('action')=='approve':
            # This service has no vendor credentials or private backend settings.
            # Freeze the implementation advertised by the actual executors.
            try:authorization=planning.rebind_scope(store,payload.get('authorization'))
            except ValueError as exc:raise DecisionError('scope_invalid',str(exc)) from exc
            filling_initial_targets=not run['tasks'] and run['graph_version']==0 and not run['authorization']['delivery_targets']
            if authorization.goal!=run['goal']:raise ValueError('Changing the top-level goal requires a new Run')
            if run['scope_confirmed'] and not filling_initial_targets and set(authorization.delivery_targets)!=set(run['authorization']['delivery_targets']):
                cutoff=int(request.get('input_through_sequence',0))
                if not cutoff or not store.query("SELECT id FROM run_inputs WHERE run_id=? AND sequence<=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],cutoff)):
                    raise ValueError('Delivery contract change requires explicit pending user input')
            owned={asset['id'] for asset in store.assets(run['project_id'])}
            if not set(authorization.allowed_assets)<=owned:raise ValueError('Foreign asset')
            if authorization.content_fingerprint!=graph.content_fingerprint(store,run['project_id']):raise DecisionError('content_changed')
            from .run_authorization import approve
            approve(store,run,authorization)
            store.execute("UPDATE workflow_decisions SET status='superseded',updated_at=? WHERE run_id=? AND id<>? AND status='pending'",
                          (store.now(),run['id'],row['id']))
            self.set_status(run,'ACTIVE')
        elif row['kind']=='delivery_scope_change' and payload.get('action')=='approve':
            try:authorization=planning.rebind_scope(store,payload.get('authorization'))
            except ValueError as exc:raise DecisionError('scope_invalid',str(exc)) from exc
            if authorization.goal!=run['goal']:raise ValueError('Delivery scope change cannot change the top-level goal')
            old_targets=set(run['authorization'].get('delivery_targets',[]))
            new_targets=set(authorization.delivery_targets)
            if not new_targets:raise ValueError('Delivery scope cannot be empty after confirmation')
            if not new_targets<=old_targets:raise ValueError('Delivery scope expansion requires a new explicit plan change')
            from .run_authorization import approve
            approve(store,run,authorization)
            superseded=store.query("SELECT task_id FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status IN ('passed','rejected','pending','running','staged')",(run['id'],))
            store.execute("UPDATE workflow_reviews SET status='superseded',updated_at=? WHERE run_id=? AND purpose='final' AND status IN ('passed','rejected','pending','running','staged')",(store.now(),run['id']))
            from .task_lifecycle import request_cleanup
            with store.connect() as db:
                for old in superseded:
                    db.execute("UPDATE control_jobs SET status='cancelled',lease_id='',lease_generation=0,updated_at=? WHERE run_id=? AND id=? AND status NOT IN ('succeeded','failed','cancelled')",
                               (store.now(),run['id'],old['task_id']))
                    request_cleanup(db,run['id'],external=False,step_id=old['task_id'])
            store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
            current=graph.get(store,run['id'])
            self.set_status(current,'ACTIVE','delivery_scope_confirmed')
            self.queue_review(graph.get(store,run['id']))
            return
        elif row['kind']=='quality' and payload.get('action')=='approve':
            request=json.loads(row['request_json']);task=next(t for t in graph.work_items(run) if t['id']==request['task_id'])
            if not any(item['kind']=='review' for item in task['spec']['acceptance']):raise ValueError('User review was not declared for this Task')
            if task['revision']!=request['revision'] or not task['quality'].get('technical_valid'):raise ValueError('Technical acceptance failed')
            if task['id'] in self.pending_task_reviews(run):raise ValueError('Agent review is still pending')
            if task['quality'].get('requires_agent') and not task['quality'].get('agent_review_passed'):raise ValueError('Agent review has not passed')
            store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json,error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",(run['id'],task['id']))
            self.set_status(run,'ACTIVE')
        else:raise ValueError('Unsupported answer')
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",(encode(payload),store.now(),row['id']))
        if payload.get('action')=='approve' and row['kind'] in ('plan_change','successor_authorization'):
            current=graph.get(store,run['id'])
            self.queue_plan(current,{'authorization_changed':row['id']})
            return
        if payload.get('action') not in ('reject','revise'):
            if store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)):
                self.set_status(run,'WAITING_USER','user_decision_pending')
            elif any(t['status']=='paused' and not t.get('control_kind') for t in graph.work_items(graph.get(store,run['id']))):
                self.set_status(run,'WAITING_USER','task_requires_resolution')

    def accept_results(self,run):
        store=self.store
        for result in store.query("SELECT * FROM task_results WHERE run_id=? AND status='staged' ORDER BY created_at,id LIMIT 100",(run['id'],)):
            task=next((t for t in graph.work_items(run) if t['id']==result['task_id']),None)
            leases=store.query('SELECT * FROM execution_leases WHERE attempt=?',(result['lease_id'],))
            accepted=bool(task and leases and task['revision']==result['task_revision'] and task['lease_id']==result['lease_id']
                          and task['lease_generation']==result['lease_generation'] and leases[0]['generation']==result['lease_generation']
                          and (run['status'] not in ('CANCELLING',*RUN_TERMINAL) or run['status']=='SUCCEEDED' and task.get('control_kind')=='MESSAGE') and task['status'] not in TERMINAL)
            if not accepted:
                store.execute("UPDATE task_results SET status='late',committed_at=? WHERE id=?",(store.now(),result['id']));continue
            outcome=result['outcome'];result_status='committed'
            try:
                data=json.loads(result['result_json'])
                if not isinstance(data,dict):raise ValueError('Task result must be an object')
                if outcome=='resource_wait':
                    graph.write_work(store,task,"UPDATE run_tasks SET status='retry_wait',next_run_at=?,wait_reason='resource',attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END WHERE run_id=? AND id=?",
                        (store.now()+5,run['id'],task['id']))
                elif task.get('control_kind'):
                    if outcome=='succeeded':
                        if task.get('control_kind')=='MESSAGE':
                            from .workflow_messages import apply as apply_message
                            apply_message(store,run,task,data)
                        elif task['spec']['profile']=='media_planner':
                            # The proposal itself is independently immutable and staged.
                            planner=store.query("SELECT status FROM planner_runs WHERE task_id=? AND run_id=?",(task['id'],run['id']))
                            if not planner or planner[0]['status']!='staged':outcome='failed';data={'error_code':'missing_proposal'}
                        elif task['spec']['profile']=='media_failure_diagnostician':self.accept_diagnosis(run,task,data)
                        else:self.accept_review(run,task,data)
                    if outcome!='succeeded':self.task_failure(run,task,data)
                elif outcome=='waiting':
                    handle=data.get('external_id') or task['external_id']
                    if not re_full_handle(handle):raise InvalidProviderHandle('Provider submission has no usable handle')
                    backoff=min(60,5*2**min(task.get('poll_count',0),4));suggested=int(data.get('poll_after',backoff))
                    delay=max(1,min(60,suggested if task.get('poll_count',0)==0 else max(backoff,suggested)))
                    graph.write_work(store,task,"UPDATE run_tasks SET status='waiting',phase='poll',external_id=?,next_run_at=?,wait_reason='provider',poll_count=poll_count+1,wait_recorded_at=COALESCE(wait_recorded_at,?),error_code='',error_detail='' WHERE run_id=? AND id=?",
                        (handle,store.now()+delay,store.now(),run['id'],task['id']))
                elif outcome=='succeeded':
                    self.accept_artifacts(run,task,data,json.loads(result['artifact_manifest']))
                else:self.task_failure(run,task,data)
            except (ValueError,KeyError,TypeError,AttributeError) as exc:
                result_status='rejected'
                code='submission_unknown' if isinstance(exc,InvalidProviderHandle) else 'invalid_result_manifest'
                from .egress_policy import scrub,secrets
                rejection={'stage':'accept_result','error_code':code,'exception_type':type(exc).__name__,
                           'message':scrub(str(exc)[:2000],secrets())}
                self.task_failure(run,task,{'error_code':code})
                graph.event(store,run,'task_result_rejected',{'task_id':task['id'],**rejection})
            store.execute('UPDATE task_results SET status=?,error_json=?,committed_at=? WHERE id=?',
                          (result_status,encode(rejection) if result_status=='rejected' else '{}',store.now(),result['id']))
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
        from .operations import get as operation
        if declared.type=='operation' and operation(declared.operation,declared.implementation).evidence_only:
            proof=report.get('verified_evidence',{});bound=task['bindings'].get('source',{})
            preview=next((item for item in report['files'] if item['name']==proof.get('preview_file')),None)
            if not preview or proof.get('source_sha256')!=bound.get('sha256') or proof.get('preview_sha256')!=preview.get('sha256') or proof.get('implementation_id')!=declared.implementation:
                raise ValueError('Evidence summary is not bound to the actual source and files')
        else:report.pop('verified_evidence',None)
        report['requires_review']=any(check.kind=='review' for check in declared.acceptance)
        report['requires_agent']=any(check.kind=='agent' for check in declared.acceptance)
        store.execute('UPDATE run_tasks SET candidate_result_json=?,quality_json=?,phase=\'validate\' WHERE run_id=? AND id=?',
                      (encode(files),encode(report),run['id'],task['id']))
        if not report.get('technical_valid'):
            self.task_failure(run,task,{'error_code':'technical_validation_failed','phase':'validate'});return
        from .workflow_inputs import roles
        spec=graph.Task.model_validate(task['spec']).execution_spec()
        if spec['kind']=='agent':spec['kind']='script'
        report['output_roles']=roles(spec,files,data.get('output_roles'))
        store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',(encode(report),run['id'],task['id']))
        if not report.get('passed'):
            self.task_failure(run,task,{'error_code':'quality_failed','phase':'validate'});return
        if report.get('requires_agent'):
            store.execute("UPDATE run_tasks SET status='reviewing' WHERE run_id=? AND id=?",(run['id'],task['id']))
            self.queue_review(graph.get(store,run['id']),task['id'])
        elif report.get('requires_review'):
            store.execute("UPDATE run_tasks SET status='reviewing' WHERE run_id=? AND id=?",(run['id'],task['id']))
            self.decision(run,'quality',{'task_id':task['id'],'revision':task['revision'],'quality':report})
        else:
            store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=?,error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",(encode(files),run['id'],task['id']))

    def task_failure(self,run,task,data):
        store=self.store;code=data.get('error_code','execution_failed');phase=data.get('phase',task['phase'])
        if task.get('control_kind')=='MESSAGE':
            from .workflow_messages import fail
            fail(store,run,task,code);return
        if phase not in ('local','sync','submit','poll','collect','validate'):phase=task['phase']
        if code not in {'submission_unknown','result_unavailable','technical_validation_failed','quality_failed','execution_failed',
                        'provider_failed','poll_failed','collect_failed','agent_failed','review_incomplete','review_contract_exhausted',
                        'planning_contract_exhausted','model_token_budget_exhausted','budget_exhausted','daily_model_quota_exhausted','missing_proposal','invalid_result_manifest','model_result','pre_submit',
                        'provider_cancelled','runtime_profile_changed','policy_changed','agent_runtime_unavailable'}:code='execution_failed'
        business_attempts=max(0,task['attempts']-task.get('infrastructure_interruptions',0))
        retry=(not task.get('control_kind') and graph.Task.model_validate(task['spec']).retry_policy=='safe_once'
               and business_attempts<2 and phase=='local' and code=='execution_failed'
               and data.get('classification') not in ('code','environment','parameters','model_result'))
        if not task.get('control_kind') and phase=='local' and code=='execution_failed' and business_attempts<3:
            try:
                from .operations import get as get_operation
                operation=get_operation(task['spec'].get('operation'),task['spec'].get('implementation')) if task['spec'].get('type')=='operation' else None
                infrastructure=data.get('classification') in ('environment','resource','network','timeout') or data.get('error_type') in ('ImportError','OSError','ConnectionError','TimeoutError','RuntimeTransportError')
                retry=retry or bool(operation and operation.retry_safe and not operation.external_side_effects and infrastructure)
            except (KeyError,ValueError):pass
        retry=retry or bool(not task.get('control_kind') and task['external_id'] and phase in ('poll','collect') and code in ('poll_failed','collect_failed') and task['recovery_count']<3)
        retry=retry or bool(task.get('type')=='agent' and code=='agent_failed' and data.get('transient') and business_attempts<3)
        message={
            'model_result':'模型未找到符合条件的结果，请调整输入或要求后重新规划。',
            'budget_exhausted':'本轮操作次数或模型预算已达上限。请调整执行范围或预算后重新发起。',
            'daily_model_quota_exhausted':'今日模型调用额度已用完。额度按用户跨 Run 共享，请等待恢复后再继续；新建一轮不会绕过每日额度。',
            'model_token_budget_exhausted':'本轮模型 Token 预算已耗尽，未继续发起模型请求。请调整范围或预算。',
            'review_contract_exhausted':'Reviewer 未能在限定纠错轮次内提交合法选择；已保留候选、检查和字段错误。',
            'planning_contract_exhausted':'Manager 未能在限定纠错轮次内提交合法计划；已保留草稿和精确校验错误。',
            'agent_failed':'专业 Agent 未能提交有效结果。已保留诊断，请检查后重试或重新规划。',
            'review_incomplete':'本轮审查未获得充分证据或有效结论，正在交回规划补充证据。',
            'submission_unknown':'外部请求结果尚未确认，已暂停以避免重复调用。',
            'pre_submit':'提交前检查未通过，尚未发起外部请求。请检查输入或配置后重新规划。',
            'result_unavailable':'执行已中断，缺少可安全恢复的结果。请先核实已有请求与产物。',
            'quality_failed':'候选成果未通过验收，已暂停，尚未正式交付。',
            'technical_validation_failed':'候选文件未通过技术检查，已暂停，尚未正式交付。',
            'poll_failed':'查询外部任务状态失败，可以重试查询已有任务。',
            'collect_failed':'获取已有任务结果失败，可以重试收取结果。',
            'provider_cancelled':'外部任务已终止，未产生可验收结果。',
            'runtime_profile_changed':'执行环境版本已变化，旧环境绑定不能由当前执行器继续运行。',
            'policy_changed':'专业 Agent 策略已变化，旧控制任务不能由当前策略继续执行。',
        }.get(code,'任务未完成，已保留执行证据。')
        if code=='execution_failed':
            detail=str(data.get('error_detail') or '')
            error_type=str(data.get('error_type') or '')
            if error_type=='ImportError' and 'opensandbox' in detail.lower():
                message='系统执行错误：OpenSandbox 运行时加载失败（ImportError）。这不是输入问题；修复运行时后可点击“继续”重试失败步骤。'
            elif data.get('classification')=='parameters':
                message='执行方案参数不符合契约：'+detail[:1000]+'。请修订方案后再执行，直接重复运行不会修正参数。'
            elif detail:
                message='系统执行错误：'+detail[:400]+'。请修复运行环境后重试。'
        if code=='agent_failed' and data.get('transient'):
            message='模型服务出现瞬时故障，系统将自动重试（第 '+str(task.get('recovery_count',0)+1)+' 次）。'
        if code=='daily_model_quota_exhausted' and data.get('quota_limit') is not None:
            import time
            hours=max(1,round((float(data.get('quota_reset_at',time.time()))-time.time())/3600,1))
            message=f"今日模型调用额度已用完（{data.get('quota_used',0)}/{data.get('quota_limit')}），预计约 {hours} 小时后恢复；新建一轮不会绕过额度。"
        if code=='agent_failed' and data.get('error_detail'):
            message='脚本执行失败：'+str(data['error_detail'])[:400]+'。已保留错误回执，请修正脚本后重试。'
        if code=='agent_runtime_unavailable':
            message='系统沙箱运行环境故障：'+str(data.get('error_detail') or '原生 Harness 无法启动')[:600]+'。不需要补充任务信息，需修复运行环境后重新执行。'
        graph.write_work(store,task,'UPDATE run_tasks SET status=?,phase=?,error_code=?,error_detail=?,recovery_count=recovery_count+?,next_run_at=? WHERE run_id=? AND id=?',
            ('retry_wait' if retry else 'paused',phase,code,message,int(retry and phase in ('poll','collect')),store.now()+min(30,2**(task['recovery_count']+1)),run['id'],task['id']))
        if task.get('control_kind') and retry:
            if task['spec'].get('profile')=='media_planner':
                store.execute("UPDATE planner_runs SET status='pending',error_code='',updated_at=? WHERE run_id=? AND task_id=?",
                              (store.now(),run['id'],task['id']))
            else:
                store.execute("UPDATE workflow_reviews SET status='pending',updated_at=? WHERE run_id=? AND task_id=?",
                              (store.now(),run['id'],task['id']))
        elif task.get('control_kind'):
            store.execute("UPDATE planner_runs SET status='rejected',error_code=?,updated_at=? WHERE run_id=? AND task_id=? AND status IN ('pending','running','staged')",(code,store.now(),run['id'],task['id']))
            store.execute("""UPDATE workflow_plan_drafts SET status='REJECTED',updated_at=?
                WHERE run_id=? AND planning_request_id=? AND status IN ('OPEN','COMMITTED')
                AND planning_request_id IN (SELECT id FROM planner_runs WHERE run_id=? AND status='rejected')""",
                (store.now(),run['id'],task['id'],run['id']))
            store.execute("UPDATE workflow_reviews SET status='rejected',updated_at=? WHERE run_id=? AND task_id=?",(store.now(),run['id'],task['id']))
        if not retry and task.get('control_kind')=='REVIEW' and code=='review_incomplete':
            review=store.query('SELECT id,subject_task FROM workflow_reviews WHERE run_id=? AND task_id=?',(run['id'],task['id']))[0]
            decision={'verdict':'inconclusive','feedback':message,'error_code':code}
            store.execute('UPDATE workflow_reviews SET decision_json=? WHERE id=?',(encode(decision),review['id']))
            if not review['subject_task'] and self.final_inconclusive_exhausted(run):
                self.set_status(run,'WAITING_USER','review_incomplete')
                return
            if review['subject_task']:
                store.execute("UPDATE run_tasks SET status='paused',error_code=?,error_detail=? WHERE run_id=? AND id=?",(code,message,run['id'],review['subject_task']))
            self.open_repair(run,review['subject_task'],'review_evidence',review['id'],code)
            store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
            self.set_status(run,'ACTIVE','evidence_planning')
            self.queue_plan(graph.get(store,run['id']),{'review_incomplete':review['id'],'task_id':review['subject_task']})
            return
        if not retry and code=='submission_unknown':
            self.decision(run,'external_submission',{'task_id':task['id'],
                'question':'外部请求结果未确认。请选择：提供供应商任务 ID 以继续查询，或确认未受理后允许使用同一请求重新提交。',
                'options':['attach_external_id','resubmit']})
            return
        if not retry:
            diagnose=(not task.get('control_kind') and task['spec'].get('on_failure')=='diagnose'
                      and code not in ('submission_unknown','budget_exhausted','daily_model_quota_exhausted','planning_contract_exhausted','model_token_budget_exhausted','result_unavailable','agent_runtime_unavailable'))
            if diagnose:
                self.open_repair(run,task['id'],'execution_failure',error_code=code)
                self.queue_review(graph.get(store,run['id']),task['id'],purpose='diagnose')
                self.set_status(graph.get(store,run['id']),'ACTIVE','failure_diagnosis')
            else:
                fatal=(code in ('result_unavailable','agent_runtime_unavailable') or task.get('control_kind') and code in {
                    'agent_failed','missing_proposal','policy_changed'})
                if fatal:
                    graph.write_work(store,task,"UPDATE run_tasks SET status='failed' WHERE run_id=? AND id=?",(run['id'],task['id']))
                    self.begin_failure(run,code)
                else:self.set_status(run,'WAITING_USER',code)

    def apply_planner(self,run):
        store=self.store
        for request in store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='staged' ORDER BY created_at",(run['id'],)):
            # The worker must have stopped before its proposals become executable.
            active=store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND slot=? AND active=1 AND expires>?',
                               (run['id'],run['id']+'|'+request['task_id'],store.now()))
            if active:continue
            cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(request['lease_id'],))
            if cleanup:continue
            owner=next(t for t in graph.work_items(run) if t['id']==request['task_id'])
            if not request['lease_id'] or owner['lease_id']!=request['lease_id'] or owner['lease_generation']!=request['lease_generation']:
                store.execute("UPDATE planner_runs SET status='rejected',error_code='stale_generation' WHERE id=?",(request['id'],))
                store.execute("UPDATE workflow_plan_drafts SET status='EXPIRED',updated_at=? WHERE planning_request_id=? AND status IN ('OPEN','COMMITTED')",(store.now(),request['id']))
                store.execute("UPDATE control_jobs SET status='failed',error_code='stale_generation' WHERE run_id=? AND id=?",(run['id'],request['task_id']))
                graph.event(store,run,'proposal_rejected',{'request_id':request['id'],'error_code':'stale_generation'})
                continue
            frozen=json.loads(request['snapshot_json'])
            if run['input_barrier_sequence'] is not None and not frozen.get('input_batch'):
                store.execute("UPDATE planner_runs SET status='rejected',error_code='new_input_barrier' WHERE id=?",(request['id'],))
                store.execute("UPDATE workflow_plan_drafts SET status='EXPIRED',updated_at=? WHERE planning_request_id=? AND status IN ('OPEN','COMMITTED')",(store.now(),request['id']))
                store.execute("UPDATE control_jobs SET status='cancelled',error_code='new_input_barrier' WHERE run_id=? AND id=?",(run['id'],request['task_id']))
                continue
            try:
                with graph.atomic_effect(store):planning.apply(store,run,request)
            except (AuthorizationRequired,ValueError) as exc:
                code='authorization_required' if isinstance(exc,AuthorizationRequired) else 'invalid_proposal'
                batch=frozen.get('input_batch')
                if batch:
                    from .run_inputs import defer_batch
                    defer_batch(store,run,batch['through_sequence'])
                store.execute('UPDATE runs SET repair_context_json=? WHERE id=?',
                    (encode({**run['repair_context'],'last_plan_error':{'code':code,'detail':str(exc)[:1000]}}),run['id']))
                store.execute("UPDATE planner_runs SET status='rejected',error_code=?,updated_at=? WHERE id=?",(code,store.now(),request['id']))
                store.execute("UPDATE workflow_plan_drafts SET status='REJECTED',updated_at=? WHERE planning_request_id=? AND status IN ('OPEN','COMMITTED')",(store.now(),request['id']))
                store.execute("UPDATE control_jobs SET status='failed',error_code=? WHERE run_id=? AND id=?",(code,run['id'],request['task_id']))
                if isinstance(exc,AuthorizationRequired):self.decision(run,'plan_change',{'planner_run':request['id'],'proposal':json.loads(request['proposal_json']).get('proposal'),'reason':str(exc),
                    'input_through_sequence':(batch or {}).get('through_sequence',0)})
                else:
                    graph.event(store,run,'proposal_rejected',{'request_id':request['id'],'error_code':code})
                    try:self.queue_plan(run,{'rejected':request['id'],'reason':str(exc)[:1000]})
                    except planning.PlanningBudgetExceeded:self.set_status(run,'WAITING_USER','planning_budget_exhausted')

    def recover(self,run):
        store=self.store;now=store.now()
        self.reconcile_model_reservations(run)
        limit=store.query('SELECT deadline_at,termination_reason FROM execution_limits WHERE task_id=?',(run['id'],))
        if limit and (limit[0]['deadline_at']<=now or limit[0]['termination_reason']):
            self.set_status(run,'CANCELLING','deadline_or_budget')
            return
        for task in graph.work_items(run):
            if task.get('control_kind')=='MESSAGE':continue
            if task['status'] not in ('ready','leased','running') or not task['lease_id']:continue
            leases=store.query('SELECT * FROM execution_leases WHERE attempt=?',(task['lease_id'],))
            if leases and leases[0]['active'] and leases[0]['expires']>now:
                if task['status']=='ready':graph.write_work(store,task,"UPDATE run_tasks SET status='running' WHERE run_id=? AND id=?",(run['id'],task['id']))
                continue
            result=store.query('SELECT status FROM task_results WHERE run_id=? AND task_id=? AND lease_generation=?',
                               (run['id'],task['id'],task['lease_generation']))
            if result:continue
            cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(task['lease_id'],))
            if cleanup:continue
            attempts=store.query('SELECT status FROM execution_attempts WHERE id=?',(task['lease_id'],))
            if attempts and attempts[0]['status']=='resource_wait':
                bindings=dict(task.get('bindings',{}))
                if task.get('control_kind'):bindings.pop('_environment',None)
                graph.write_work(store,task,"UPDATE run_tasks SET status='ready',lease_id='',bindings_json=? WHERE run_id=? AND id=?",
                                 (encode(bindings),run['id'],task['id']))
                continue
            checkpoint=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                                   (run['id'],task['id'],task['revision']))
            if checkpoint and task['phase'] in ('local','sync'):
                manifest=json.loads(checkpoint[0]['manifest'])
                if manifest:
                    graph.write_work(store,task,"UPDATE run_tasks SET candidate_result_json=?,phase='validate',status='ready',lease_id='',infrastructure_interruptions=infrastructure_interruptions+1 WHERE run_id=? AND id=?",
                                     (encode(list(manifest)),run['id'],task['id']))
                    continue
            if task.get('control_kind'):
                staged=store.query("SELECT id FROM planner_runs WHERE run_id=? AND task_id=? AND status='staged'",(run['id'],task['id']))
                if staged:continue
                from .specialist_profiles import get as current_profile
                try:compatible=digest(task['profile'])==digest(current_profile(task['spec']['profile']).snapshot())
                except (KeyError,ValueError):compatible=False
                if not compatible:
                    self.task_failure(run,task,{'error_code':'policy_changed','phase':task['phase']})
                    continue
                safe=task['infrastructure_interruptions']<3
            else:
                spec=graph.Task.model_validate(task['spec']).execution_spec()
                if spec['kind']=='agent':safe=task['infrastructure_interruptions']<3
                else:
                    from .operation_lifecycle import for_step,recover_dynamic
                    implementation=for_step(spec)
                    calls=store.query('SELECT state,result,external_id FROM workflow_provider_calls WHERE request_key=?',
                                      (run['id']+':'+task['id']+':'+str(task['revision']),))
                    recovery=recover_dynamic(implementation,{**task,'spec':spec},calls[0] if calls else None)
                    if recovery['action']=='poll':
                        graph.write_work(store,task,"UPDATE run_tasks SET external_id=?,phase='poll',status='ready',lease_id='',infrastructure_interruptions=infrastructure_interruptions+1 WHERE run_id=? AND id=?",(recovery['external_id'],run['id'],task['id']));continue
                    if recovery['action']=='validate':
                        graph.write_work(store,task,"UPDATE run_tasks SET candidate_result_json=?,phase='validate',status='ready',lease_id='',infrastructure_interruptions=infrastructure_interruptions+1 WHERE run_id=? AND id=?",(encode(recovery['candidate']),run['id'],task['id']));continue
                    if recovery['action']=='failed':
                        self.task_failure(run,task,{'error_code':recovery['error_code'],'phase':task['phase']});continue
                    if recovery['action']=='unknown':
                        self.task_failure(run,task,{'error_code':'submission_unknown','phase':task['phase']});continue
                    safe=recovery['action']=='ready' and task['infrastructure_interruptions']<3
            if safe:
                bindings=dict(task.get('bindings',{}))
                if task.get('control_kind'):bindings.pop('_environment',None)
                graph.write_work(store,task,"UPDATE run_tasks SET status='ready',lease_id='',bindings_json=?,infrastructure_interruptions=infrastructure_interruptions+1,next_run_at=? WHERE run_id=? AND id=?",
                                 (encode(bindings),now,run['id'],task['id']))
            else:self.task_failure(run,task,{'error_code':'result_unavailable'})

        current=graph.get(store,run['id'])
        profiles=set()
        for worker in store.query('''SELECT c.settings FROM execution_scheduler_configs c JOIN execution_workers w ON w.id=c.worker
            WHERE w.draining=0 AND w.heartbeat>?''',(store.now()-30,)):
            try:profile=json.loads(worker['settings']).get('runtime_profile')
            except (TypeError,ValueError):profile=None
            if profile:profiles.add(profile)
        for task in graph.work_items(current):
            if task.get('control_kind') or task['status']!='ready' or task['lease_id']:continue
            bound=task.get('bindings',{}).get('_environment')
            if not bound or not profiles or bound in profiles:continue
            graph.write_work(store,task,"UPDATE run_tasks SET status='paused',error_code='runtime_profile_changed',error_detail=? WHERE run_id=? AND id=?",
                             ('执行环境版本已变化，需要创建新 revision 后继续。',run['id'],task['id']))
            self.open_repair(current,task['id'],'runtime_profile_changed',error_code='runtime_profile_changed')
            store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
            self.set_status(current,'ACTIVE','repair_planning')
            self.queue_plan(graph.get(store,run['id']),{'runtime_profile_changed':task['id'],'task_id':task['id']})
            break

    def reconcile_model_reservations(self,run):
        store=self.store
        from .task_lifecycle import expire_leases
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');expire_leases(db,run['id'])
        rows=store.query('''SELECT r.id,r.attempt_id,r.reserved_tokens,r.input_reserved_tokens,r.output_reserved_tokens
            FROM workflow_model_reservations r JOIN execution_attempts a ON a.id=r.attempt_id
            WHERE r.run_id=? AND r.state='reserved' AND NOT EXISTS(
                SELECT 1 FROM execution_leases l WHERE l.attempt=r.attempt_id AND l.active=1 AND l.expires>?)''',
            (run['id'],store.now()))
        for row in rows:
            # A reservation is an estimate, not a provider receipt. A killed
            # request without a trusted receipt cannot settle estimated input
            # as actual usage; retain the bounded unknown envelope instead.
            used=row['reserved_tokens'];state='interrupted_usage_unknown';input_settled=None
            store.execute('''UPDATE workflow_model_reservations SET used_tokens=?,state=?,
                input_settled_tokens=?,output_settled_tokens=NULL WHERE id=? AND state='reserved' ''',
                (used,state,input_settled,row['id']))
            graph.event(store,run,'model_reservation_reconciled',{'attempt_id':row['attempt_id'],'outcome':state})

    def pending_task_reviews(self,run):
        return {row['subject_task'] for row in self.store.query(
            "SELECT subject_task FROM workflow_reviews WHERE run_id=? AND purpose='task' AND status IN ('pending','running','staged')",
            (run['id'],))}

    def advance(self,run):
        store=self.store;now=store.now()
        if store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)):
            self.set_status(run,'WAITING_USER','user_decision_pending');return
        # Manual reviews are presented one at a time, but parallel candidates
        # must not lose their pending confirmation when the first is answered.
        pending_reviews=self.pending_task_reviews(run)
        pending=next((t for t in graph.work_items(run) if t['status']=='reviewing' and t['quality'].get('requires_review') and t['id'] not in pending_reviews
                      and (not t['quality'].get('requires_agent') or t['quality'].get('agent_review_passed'))),None)
        if pending:
            self.decision(run,'quality',{'task_id':pending['id'],'revision':pending['revision'],'quality':pending['quality']});return
        done={t['id'] for t in graph.work_items(run) if t['status']=='succeeded'}
        self.advance_ready(run)
        if run['input_barrier_sequence'] is not None:return
        self.advance_graph(run,done)

    def advance_ready(self,run):
        if run['terminal_reason'] in ('user_paused','user_rejected'):return
        store=self.store;now=store.now()
        done={t['id'] for t in graph.work_items(run) if t['status']=='succeeded'}
        from .run_inputs import can_dispatch
        for task in graph.work_items(run):
            if not can_dispatch(run,task):continue
            parents={d['upstream_task_id'] for d in run['dependencies'] if d['downstream_task_id']==task['id']}
            if task['status'] in ('blocked','retry_wait','waiting') and task['next_run_at']<=now and parents<=done:
                if task['lease_id']:
                    active=store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',(task['lease_id'],now))
                    cleaning=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(task['lease_id'],))
                    if active or cleaning:continue
                graph.write_work(store,task,"UPDATE run_tasks SET status='ready',lease_id='',wait_reason='' WHERE run_id=? AND id=?",(run['id'],task['id']))

    def advance_graph(self,run,done):
        store=self.store;now=store.now()
        active_planners=store.query("SELECT id FROM planner_runs WHERE run_id=? AND status IN ('pending','running','staged')",(run['id'],))
        business=[t for t in graph.work_items(run) if not t.get('control_kind')]
        context=run.get('repair_context',{})
        if not run['sealed'] and context.get('active') and not active_planners:
            triggers=[next((t for t in business if t['id']==identifier),None) for identifier in run['replan_after']]
            waiting=any(t and t['status'] in ('blocked','ready','leased','running','waiting','retry_wait','reviewing') for t in triggers)
            if not waiting:
                try:self.queue_plan(run,{'repair_resume':context.get('source','repair'),'task_id':context.get('subject_task','')})
                except planning.PlanningBudgetExceeded:self.set_status(run,'FAILED','planning_budget_exhausted')
                return
        if run['sealed'] and any(t['status']=='paused' for t in business):
            diagnosing=any(t.get('control_kind')=='DIAGNOSE' and t['status'] in ('ready','running','retry_wait') for t in graph.work_items(run))
            self.set_status(run,'ACTIVE' if diagnosing else 'WAITING_USER','failure_diagnosis' if diagnosing else 'sealed_graph_unresolved');return
        if not run['sealed'] and not active_planners:
            ready=not business or set(run['replan_after'])<=done
            if ready:
                try:self.queue_plan(run,{'completed':sorted(done)})
                except planning.PlanningBudgetExceeded:self.set_status(run,'WAITING_USER','planning_budget_exhausted')
        if run['sealed'] and business and all(t['status']=='succeeded' for t in business) and not active_planners:
            from .workflow_messages import business_inflight
            busy,cleanup=business_inflight(store,run['id'])
            external=store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],))
            decisions=store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))
            if not busy and not cleanup and not external and not decisions:
                reviews=store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final' AND graph_version=? ORDER BY created_at DESC",(run['id'],run['graph_version']))
                if reviews and reviews[0]['status']=='passed':self.publish(run,reviews[0])
                elif not reviews:self.queue_review(run)

    def queue_review(self,run,subject='',purpose=None,*,evidence_tasks=()):
        store=self.store;purpose=purpose or ('task' if subject else 'final')
        from .specialist_profiles import get as profile
        policy='media_failure_diagnostician' if purpose=='diagnose' else 'visual_quality_reviewer' if subject else 'delivery_reviewer'
        if purpose=='diagnose' and run['authorization']['agent_profile_digests'].get(policy)!=digest(profile(policy).snapshot()):
            self.set_status(run,'WAITING_USER','diagnostic_policy_not_authorized');return
        existing=store.query("SELECT id FROM workflow_reviews WHERE run_id=? AND purpose=? AND subject_task=? AND status IN ('pending','running','staged')",(run['id'],purpose,subject))
        if existing:return
        identifier=uuid.uuid4().hex;task_id=identifier;now=store.now()
        frozen={'graph_version':run['graph_version'],'authorization_version':run['authorization_version'],
                'goal':run['goal'],'delivery_targets':run['authorization']['delivery_targets'],'sealed':run['sealed'],
                'tasks':[t for t in graph.work_items(run) if not t.get('control_kind') and (not subject or t['id']==subject)]}
        asset_ids=run['authorization'].get('allowed_assets',[])
        frozen['input_asset_names']=[]
        if asset_ids:
            placeholders=','.join('?' for _ in asset_ids)
            for asset in store.query(f'SELECT name,filename FROM assets WHERE id IN ({placeholders})',tuple(asset_ids)):
                frozen['input_asset_names'].extend(value for value in (asset.get('name'),asset.get('filename')) if value)
        frozen['input_asset_names']=list(dict.fromkeys(frozen['input_asset_names']))
        frozen['review_scope']=purpose
        if purpose=='final':
            from .effective_delivery import confirmed_scope
            frozen['effective_delivery_scope']=confirmed_scope(store,run)
        if purpose!='diagnose':
            from .workflow_history import plan_confirmation_evidence
            frozen['plan_confirmation_evidence']=plan_confirmation_evidence(store,run)
        from .operations import get as get_operation
        operation_contracts={item['id']:get_operation(item['spec']['operation'],item['spec'].get('implementation')).public()
            for item in frozen['tasks'] if item['type']=='operation'}
        frozen['operation_output_contracts']={key:value['output_contract'] for key,value in operation_contracts.items()}
        frozen['operation_execution_contracts']={key:{field:value[field] for field in
            ('implementation','lifecycle','executor','model_profile','external_side_effects','provider_identity','execution_mode')}
            for key,value in operation_contracts.items()}
        if purpose=='final':
            complete_tasks=frozen['tasks']
            frozen['dependencies']=run['dependencies']
            frozen['supporting_tasks']=[{'id':task['id'],'revision':task['revision'],'status':task['status'],
                'objective':task['spec']['objective'],'output_roles':task.get('quality',{}).get('output_roles',{})}
                for task in complete_tasks if not task['spec'].get('delivery_stage') and task['spec'].get('purpose')!='evidence']
            frozen['provider_receipts']=[]
            for call in store.query('''SELECT request_key,step,revision,provider,operation,state,external_id,implementation
                    FROM workflow_provider_calls WHERE workflow=? ORDER BY created''',(run['id'],)):
                implementation=json.loads(call.pop('implementation'))
                frozen['provider_receipts'].append({**call,**{key:implementation.get(key,'') for key in
                    ('provider_identity','route_adapter','model')}})
            frozen['delivery_provenance']=[{
                'task_id':task['id'],'revision':task['revision'],'status':task['status'],
                'delivery_stage':task['spec'].get('delivery_stage',False),
                'accepted_outputs':task['accepted_result'],
                'producer':{key:task['spec'].get(key) for key in ('type','operation','implementation','profile')},
                'operation_execution':frozen['operation_execution_contracts'].get(task['id']),
                'input_sources':task['spec'].get('inputs',{}),
                'current_provider_receipts':[call for call in frozen['provider_receipts']
                    if call['step']==task['id'] and call['revision']==task['revision']],
                'scope':'Current selected producer and declared inputs, including upstream processing. '
                    'Succeeded registered local operations use their model_profile and accepted outputs as execution evidence; '
                    'they do not create external provider receipts. Unrelated historical calls are not provenance.'}
                for task in complete_tasks if task['spec'].get('purpose')!='evidence']
            from .review_identity import applied_user_changes
            frozen['user_changes'],frozen['user_changes_truncated']=applied_user_changes(store,run)
            from .workflow_history import snapshot as history_snapshot,user_reviews
            frozen['user_reviews']=user_reviews(store,run)
            frozen['history']=history_snapshot(store,run)
            frozen['failure_facts']=[{'task_id':item['step'],'revision':item['revision'],
                'diagnostic':json.loads(item['record'])} for item in store.query(
                    'SELECT step,revision,record FROM workflow_diagnostics WHERE workflow=? ORDER BY created DESC LIMIT 100',(run['id'],))]
            from .review_identity import final_criteria
            frozen['review_criteria']=final_criteria(run,complete_tasks)
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
            selected=next(t for t in graph.work_items(run) if t['id']==subject)
            for ref in selected['spec'].get('inputs',{}).values():
                if ref.get('step'):
                    parent=next(t for t in graph.work_items(run) if t['id']==ref['step'])
                    path=parent['quality'].get('output_roles',{}).get(ref['role'])
                    if path and parent['status']=='succeeded':frozen['upstream'].append({**parent,'accepted_result':[path],'candidate_result':[]})
        evidence_ids=set(evidence_tasks)
        if purpose=='final':evidence_ids.update(task['id'] for task in graph.active_tasks(run) if task['spec']['purpose']=='evidence' and task['status']=='succeeded')
        frozen['evidence_tasks']=[task for task in graph.active_tasks(run) if task['id'] in evidence_ids]
        if any(task['status']!='succeeded' or not task['quality'].get('verified_evidence') for task in frozen['evidence_tasks']):raise ValueError('Review evidence is not verified')
        if purpose=='final':
            from .workflow_agent_contracts import delivery_publication_names
            frozen['publication_names']=delivery_publication_names(frozen)
        from .review_identity import identity
        review_identity=identity(store,run,frozen)
        prior=store.query('SELECT * FROM workflow_reviews WHERE run_id=? AND purpose=? AND review_identity=?',(run['id'],purpose,review_identity))
        if prior:
            if prior[0]['status'] in ('pending','running','staged'):return
            if prior[0]['status']=='passed' and purpose=='final':
                approved={name for task in run['tasks'] for name in task['accepted_result']}
                if set(json.loads(prior[0]['decision_json']).get('deliverables',[]))<=approved:
                    self.publish(run,prior[0]);return
            previous=json.loads(prior[0]['decision_json'])
            feedback=(previous.get('feedback') or '上次审查尚未证明当前结果满足已确认要求。')[:1600]
            if previous.get('verdict')=='pass':feedback='当前产物引用与先前通过的交付选择不一致，无法沿用旧交付。'
            if subject:
                selected=next(task for task in run['tasks'] if task['id']==subject)
                self.decision(run,'quality_feedback',{'task_id':subject,'revision':selected['revision'],
                    'reason':'候选和证据未变化，不能原样重复审查。上次反馈：'+feedback,'previous_review_id':prior[0]['id']})
            else:self.decision(run,'input_question',{'reason':'相同候选、标准和证据已有审查结论，不能重复请求内容判断。',
                'question':'当前阻塞原因：'+feedback+'\n如果要继续，请针对上述问题说明修改要求或补充可验证的证据；也可以停止本次任务。原样重复提交不会改变审查结果。','previous_review_id':prior[0]['id']})
            graph.event(store,run,'review_reused_or_blocked',{'review_identity':review_identity,'previous_review_id':prior[0]['id']})
            return
        spec={'profile':policy,'request_id':identifier,
              'snapshot_digest':digest(frozen),'objective':frozen['goal']}
        store.execute('''INSERT INTO workflow_reviews(id,run_id,task_id,purpose,subject_task,graph_version,review_identity,
            snapshot_json,snapshot_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)''',
            (identifier,run['id'],task_id,purpose,subject,run['graph_version'],review_identity,encode(frozen),digest(frozen),now,now))
        store.execute('''INSERT INTO control_jobs(run_id,id,type,authorization_version,control_kind,snapshot_json,snapshot_digest,spec_json,profile_json,status,created_graph_version,created_at,updated_at)
            VALUES (?,?,'agent',?,?,?,?,?,?,'ready',?,?,?)''',(run['id'],task_id,run['authorization_version'],'DIAGNOSE' if purpose=='diagnose' else 'REVIEW',encode(frozen),digest(frozen),encode(spec),encode(profile(spec['profile']).snapshot()),run['graph_version'],now,now))
        if not subject or purpose=='diagnose':self.set_status(run,'ACTIVE')
        graph.event(store,run,'review_requested',{'request_id':identifier,'purpose':purpose,'task_id':subject})

    def accept_diagnosis(self,run,task,data):
        from .workflow_agent_contracts import Diagnosis
        store=self.store
        rows=store.query("SELECT * FROM workflow_reviews WHERE id=? AND run_id=? AND purpose='diagnose'",(task['spec']['request_id'],run['id']))
        if not rows or data.get('snapshot_digest')!=rows[0]['snapshot_digest']:raise ValueError('Diagnostic snapshot changed')
        diagnosis=Diagnosis.model_validate(data['diagnosis']).model_dump()
        store.execute("UPDATE workflow_reviews SET status='completed',decision_json=?,updated_at=? WHERE id=?",(encode(diagnosis),store.now(),rows[0]['id']))
        store.execute("UPDATE control_jobs SET status='succeeded',error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",(run['id'],task['id']))
        frozen=json.loads(rows[0]['snapshot_json'])
        prior=next((t for t in frozen['tasks'] if t['id']==rows[0]['subject_task']),None)
        subject=next((t for t in graph.work_items(run) if t['id']==rows[0]['subject_task']),None)
        if not prior or not subject or subject['revision']!=prior['revision'] or not subject.get('selected',1):
            graph.event(store,run,'diagnosis_stale',{'task_id':rows[0]['subject_task'],'request_id':rows[0]['id'],
                'diagnosed_revision':prior['revision'] if prior else None,
                'current_revision':subject['revision'] if subject else None})
            return
        from .workflow_repair import resolve
        resolution=resolve(subject['error_code'],subject['phase'])
        if resolution.action=='manager_revision':
            self.open_repair(run,subject['id'],'diagnosis',rows[0]['id'],subject['error_code'])
            store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
            self.set_status(run,'ACTIVE','repair_planning')
            self.queue_plan(graph.get(store,run['id']),{'failure_diagnosed':rows[0]['id'],'task_id':subject['id'],
                'revision':subject['revision'],'error_code':subject['error_code'],'diagnosis':diagnosis,
                'repair_policy':resolution.action})
        elif resolution.action=='retry_same_revision':
            store.execute("UPDATE run_tasks SET status='retry_wait',next_run_at=?,wait_reason='deterministic_recovery',error_code='',error_detail='' WHERE run_id=? AND id=?",
                          (store.now(),run['id'],subject['id']))
            self.set_status(run,'ACTIVE','deterministic_recovery')
        else:self.set_status(run,'WAITING_USER',resolution.action)
        graph.event(store,run,'diagnosis_completed',{'task_id':rows[0]['subject_task'],'request_id':rows[0]['id']})

    def final_inconclusive_exhausted(self,run):
        prior=self.store.query("SELECT decision_json FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='rejected'",(run['id'],))
        return sum(json.loads(item['decision_json'] or '{}').get('verdict')=='inconclusive' for item in prior)>=2

    def accept_review(self,run,task,data):
        store=self.store
        rows=store.query('SELECT * FROM workflow_reviews WHERE id=? AND run_id=?',(task['spec']['request_id'],run['id']))
        if not rows:raise ValueError('Unknown review')
        if rows[0]['status'] not in ('pending','running','staged'):raise ValueError('Review is no longer pending')
        from .workflow_agent_contracts import ResolvedReviewDecision
        row=rows[0];decision=ResolvedReviewDecision.model_validate(data.get('decision',{})).model_dump()
        if decision.get('snapshot_digest')!=row['snapshot_digest']:raise ValueError('Review snapshot mismatch')
        if row['purpose']=='final' and (row['graph_version']!=run['graph_version'] or not run['sealed']):raise ValueError('Final review graph changed')
        if row['subject_task']:
            frozen=json.loads(row['snapshot_json'])
            prior=next(t for t in frozen['tasks'] if t['id']==row['subject_task'])
            current=next(t for t in graph.work_items(run) if t['id']==row['subject_task'])
            if any(prior[key]!=current[key] for key in ('spec','revision','candidate_result','quality')):
                raise ValueError('Reviewed task inputs or candidate changed')
        from .workflow_agent_contracts import validate_resolved_review
        inspections={r['id']:r['reference'] for r in store.query(
            'SELECT id,reference FROM workflow_inspections WHERE run_id=? AND task_id=? AND attempt_id=? AND snapshot_digest=?',
            (run['id'],task['id'],task['lease_id'],row['snapshot_digest']))}
        validate_resolved_review(decision,json.loads(row['snapshot_json']),inspections)
        from .workflow_agent_contracts import validate_temporal_evidence
        records=store.query('SELECT id,reference,evidence_json FROM workflow_inspections WHERE run_id=? AND task_id=? AND attempt_id=? AND snapshot_digest=?',
            (run['id'],task['id'],task['lease_id'],row['snapshot_digest']))
        validate_temporal_evidence(decision,json.loads(row['snapshot_json']),records)
        passed=decision['verdict']=='pass'
        subject=None
        if passed and row['subject_task']:
            subject=next(t for t in graph.work_items(graph.get(store,run['id'])) if t['id']==row['subject_task'])
            if not subject['quality'].get('technical_valid') or not subject['quality'].get('passed'):
                raise ValueError('Review cannot override program acceptance failure')
        store.execute('UPDATE workflow_reviews SET status=?,decision_json=?,updated_at=? WHERE id=?',('passed' if passed else 'rejected',encode(decision),store.now(),row['id']))
        store.execute("UPDATE control_jobs SET status='succeeded',error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",(run['id'],task['id']))
        if passed and row['subject_task']:
            quality={**subject['quality'],'agent_review_passed':True}
            store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',(encode(quality),run['id'],subject['id']))
            if quality.get('requires_review'):
                self.decision(run,'quality',{'task_id':subject['id'],'revision':subject['revision'],'quality':quality})
            else:store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json,error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",(run['id'],subject['id']))
        elif not passed:
            if row['purpose']=='final':
                inconclusive=decision['verdict']=='inconclusive'
                code='review_incomplete' if inconclusive else 'quality_failed'
                self.open_repair(run,'','review_evidence' if inconclusive else 'final_review',row['id'],code)
                store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
                if inconclusive and self.final_inconclusive_exhausted(run):
                    self.set_status(run,'WAITING_USER','review_incomplete')
                    return
                self.set_status(run,'ACTIVE','evidence_planning' if inconclusive else 'final_review_rejected')
                self.queue_plan(graph.get(store,run['id']),{
                    ('review_incomplete' if inconclusive else 'review_rejected'):row['id'],
                    'task_id':'','decision':decision,
                    'repair_policy':'evidence' if inconclusive else 'manager_revision'})
            else:
                inconclusive=decision['verdict']=='inconclusive'
                code='review_incomplete' if inconclusive else 'quality_failed'
                prefix='内容证据不足：' if inconclusive else '内容验收未通过：'
                store.execute("UPDATE run_tasks SET status='paused',error_code=?,error_detail=? WHERE run_id=? AND id=?",
                    (code,prefix+(decision.get('feedback') or '请检查候选并重新规划。')[:1600],run['id'],row['subject_task']))
                self.open_repair(run,row['subject_task'],'review_evidence' if inconclusive else 'task_review',row['id'],code)
                store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
                self.set_status(run,'ACTIVE','evidence_planning' if inconclusive else 'task_review_rejected')
                self.queue_plan(graph.get(store,run['id']),{
                    ('review_incomplete' if inconclusive else 'task_review_rejected'):row['id'],
                    'task_id':row['subject_task'],'decision':decision,
                    'repair_policy':'evidence' if inconclusive else 'manager_revision'})

    def publish(self,run,review):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._publish(run,review)

    def _publish(self,run,review):
        store=self.store
        run=graph.get(store,run['id'])
        if run['delivered_at'] is not None or run['input_barrier_sequence'] is not None:return
        if not run['sealed'] or run['status'] in ('CANCELLING','CANCELLED','FAILED'):return
        if any(task['status']!='succeeded' for task in graph.active_tasks(run)):return
        if store.query("SELECT id FROM run_inputs WHERE run_id=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],)):return
        if store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)):
            self.set_status(run,'WAITING_USER','user_decision_pending');return
        authoritative=store.query("""SELECT * FROM workflow_reviews WHERE id=? AND run_id=? AND purpose='final'
            AND subject_task='' AND status='passed'""",(review.get('id'),run['id']))
        if authoritative:
            from .review_identity import final_identity
            if authoritative[0]['review_identity']!=final_identity(store,run):
                # User requirements or scope can change without changing the
                # DAG counter. Never publish an older content approval.
                graph.event(store,run,'delivery_review_outdated',{'review_id':authoritative[0]['id']})
                self.queue_review(run)
                return
        if not authoritative:
            graph.event(store,run,'delivery_blocked',{'reason':'invalid_final_review'})
            self.set_status(run,'WAITING_USER','invalid_delivery_manifest')
            return
        review=authoritative[0]
        decision=json.loads(review['decision_json']);selected=decision.get('deliverables',[])
        from .workflow_agent_contracts import approved_delivery_refs,delivery_leaf_refs,delivery_publication_names
        from .workflow_model_protocol import required_delivery_extensions,required_delivery_names,required_delivery_roles
        scope={**delivery_scope(store,run),'tasks':graph.active_tasks(run)}
        required=required_delivery_extensions(scope);required_names=required_delivery_names(scope);required_roles=required_delivery_roles(scope)
        eligible=approved_delivery_refs(scope)
        publication_names=delivery_publication_names(scope)
        accepted={name:t for t in graph.active_tasks(run) if t['spec']['delivery_stage'] for name in t['accepted_result'] if name in eligible}
        if not selected or not isinstance(selected,list) or any(name not in accepted for name in selected):
            self.set_status(run,'WAITING_USER','invalid_delivery_manifest');return
        primary=decision.get('primary') or (selected[0] if len(set(selected))==1 else '')
        if primary not in selected:
            self.set_status(run,'WAITING_USER','invalid_delivery_manifest');return
        missing_tasks={identifier for identifier,refs in delivery_leaf_refs(scope).items() if not set(selected).intersection(refs)}
        if missing_tasks:
            graph.event(store,run,'delivery_blocked',{'reason':'delivery_targets_incomplete',
                'missing_task_ids':sorted(missing_tasks),'missing_extensions':[]})
            self.set_status(run,'WAITING_USER','delivery_targets_incomplete');return
        aliases={'jpg':'jpeg'}
        delivered={aliases.get(name.rsplit('.',1)[-1].lower(),name.rsplit('.',1)[-1].lower()) for name in selected if '.' in name}
        missing_extensions=required-delivered
        if missing_extensions:
            graph.event(store,run,'delivery_blocked',{'reason':'delivery_targets_incomplete',
                'missing_task_ids':[],'missing_extensions':sorted(missing_extensions)})
            if store.query("SELECT id FROM run_inputs WHERE run_id=? AND status='APPLIED' LIMIT 1",(run['id'],)):
                self.decision(run,'delivery_scope_change',{
                    'question':'当前成果未满足全部交付目标。请核对交付范围；已有合格成果可以复用。',
                    'current_targets':run['authorization'].get('delivery_targets',[]),
                    'missing_extensions':sorted(missing_extensions),
                    'reason':'已有后续输入，但当前成果仍未覆盖已确认的交付范围。保留原范围时应补齐缺少的交付。'})
            self.set_status(run,'WAITING_USER','delivery_targets_incomplete');return
        from pathlib import PurePosixPath
        missing_names=required_names-{publication_names.get(name,PurePosixPath(name).name) for name in selected}
        if missing_names:
            graph.event(store,run,'delivery_blocked',{'reason':'delivery_targets_incomplete',
                'missing_task_ids':[],'missing_extensions':[],'missing_names':sorted(missing_names)})
            self.set_status(run,'WAITING_USER','delivery_targets_incomplete');return
        delivered_roles={role for name in selected for role,path in accepted[name].get('quality',{}).get('output_roles',{}).items() if path==name}
        missing_roles=required_roles-delivered_roles
        if missing_roles:
            graph.event(store,run,'delivery_blocked',{'reason':'delivery_targets_incomplete',
                'missing_task_ids':[],'missing_extensions':[],'missing_roles':sorted(missing_roles)})
            self.set_status(run,'WAITING_USER','delivery_targets_incomplete');return
        artifacts=[];publications=[]
        import mimetypes
        from pathlib import PurePosixPath
        for name in dict.fromkeys(selected):
            task=accepted[name]
            rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                             (run['id'],task['id'],task['revision']))
            record=json.loads(rows[0]['manifest']).get(name) if rows else None
            if not record:raise ValueError('Delivery lacks a durable checkpoint')
            display_name=publication_names.get(name,PurePosixPath(name).name)
            filename=task['id']+'-'+record['artifact'][:12]+'-'+display_name
            logical='edit/agent_exports/'+run['id']+'/'+filename
            if not store.shared_files:raise ValueError('Dynamic delivery requires shared immutable artifact storage')
            obj=store.artifacts.record(run['project_id'],record['artifact'])
            if obj['sha256']!=record['sha256']:raise ValueError('Delivery changed after review')
            publications.append((logical,record['artifact']))
            artifacts.append({'file':filename,'name':display_name,'size':record['size'],'sha256':record['sha256'],
                'artifact_id':record['artifact'],
                'type':mimetypes.guess_type(filename)[0] or 'application/octet-stream','role':'deliverable',
                'quality_state':'approved','workflow_delivery':True,'source_step':task['id'],'source_revision':task['revision'],
                'is_primary':name==primary})
        # The explicit, reviewed immutable reference owns primary selection.
        # Shared ordering only arranges the remaining approved deliverables.
        from .artifacts import artifact_key
        # Keep lossless PNG before equivalent JPEG among non-primary attachments.
        artifacts.sort(key=lambda item:(artifact_key(item)[:-1],
            PurePosixPath(item['name']).suffix.lower()!='.png',artifact_key(item)[-1]))
        try:store.check_storage(run['project_id'],sum(a['size'] for a in artifacts),replace_turn=run['id'])
        except ValueError as exc:
            self.store.execute('UPDATE runs SET repair_context_json=? WHERE id=?',(encode({'active':False,'error_code':'storage_quota_exceeded','message':str(exc)[:500]}),run['id']))
            self.set_status(run,'WAITING_USER','storage_quota_exceeded');return
        for logical,artifact in publications:store.artifacts.publish(run['project_id'],logical,artifact)
        if store.execute("UPDATE runs SET delivered_at=?,delivery_json=?,status='SUCCEEDED',updated_at=? WHERE id=? AND delivered_at IS NULL AND sealed=1 AND input_barrier_sequence IS NULL",
                         (store.now(),encode(artifacts),store.now(),run['id'])):
            graph.event(store,run,'delivered',{'files':[a['file'] for a in artifacts],
                'review_id':review.get('id',''),'review_identity':review.get('review_identity',''),
                'review_graph_version':review.get('graph_version',run['graph_version'])})

    def cancel(self,run):
        store=self.store
        from .workflow_messages import pending,fail
        for message in pending(store,run['id']):
            fail(store,run,{'id':'message_'+message['id'],'spec':{'message_input_id':message['id']}},'run_cancelled')
        from .task_lifecycle import request_cleanup
        with store.connect() as db:request_cleanup(db,run['id'])
        store.execute("UPDATE run_inputs SET status='FAILED',error_code='run_cancelled' WHERE run_id=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],))
        store.execute("UPDATE run_tasks SET status='cancelled' WHERE run_id=? AND selected=1 AND status NOT IN ('succeeded','failed','cancelled')",(run['id'],))
        store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=? AND status NOT IN ('succeeded','failed','cancelled')",(run['id'],))
        store.execute("UPDATE planner_runs SET status='rejected',error_code='cancelled' WHERE run_id=? AND status IN ('pending','running','staged')",(run['id'],))
        store.execute("""UPDATE workflow_plan_drafts SET status='REJECTED',updated_at=?
            WHERE run_id=? AND status IN ('OPEN','COMMITTED')
            AND planning_request_id IN (SELECT id FROM planner_runs WHERE run_id=? AND status='rejected')""",
            (store.now(),run['id'],run['id']))
        store.execute("UPDATE workflow_reviews SET status='cancelled' WHERE run_id=? AND status IN ('pending','running','staged')",(run['id'],))
        store.execute("UPDATE workflow_decisions SET status='cancelled' WHERE run_id=? AND status='pending'",(run['id'],))
        busy=store.query("SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?",(run['id'],store.now()))
        cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],))
        if not busy and not cleanup:
            terminal='CANCELLED' if run['terminal_reason']=='user_cancelled' else 'FAILED'
            self.set_status(run,terminal,run['terminal_reason'])

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
                        with bind(task_id=run['id'],run_id=identifier,reconcile_generation=generation):
                            self.reconcile(identifier,generation)
                            emit('workflow.reconciled',duration_seconds=time.monotonic()-started,graph_version=run['graph_version'])
                        self.store.execute("UPDATE runs SET reconcile_error_signature='',reconcile_error_count=0 WHERE id=?",(identifier,))
                    except ReconcileLost:pass
                    except Exception as exc:
                        failed=True;code=getattr(exc,'sqlstate','') or getattr(exc,'code','') or ''
                        signature=digest({'type':type(exc).__name__,'code':str(code)[:120],'message':str(exc)[:500]})
                        try:self.store.execute('INSERT INTO workflow_reconcile_failures VALUES (?,?,?,?,?,?)',
                            (uuid.uuid4().hex,identifier,generation,type(exc).__name__,str(code)[:120],self.store.now()))
                        except Exception as record_error:emit('workflow.reconcile_failure_record_failed','ERROR',exc=record_error,run_id=identifier)
                        try:
                            current=graph.get(self.store,identifier)
                            repeated=current['reconcile_error_count']+1 if current['reconcile_error_signature']==signature else 1
                            terminal=repeated>=8
                            with self.store.connect() as db:
                                db.execute('''UPDATE runs SET reconcile_error_signature=?,reconcile_error_count=?,
                                    next_reconcile_at=?,status=CASE WHEN ? AND status<>'CANCELLING' THEN 'CANCELLING' ELSE status END,
                                    terminal_reason=CASE WHEN ? AND status<>'CANCELLING' THEN 'reconcile_failed' ELSE terminal_reason END
                                    WHERE id=?''',(signature,repeated,self.store.now()+min(60,2**min(repeated,6)),terminal,terminal,identifier))
                                if terminal:
                                    from .task_lifecycle import request_cleanup
                                    request_cleanup(db,identifier)
                            if terminal:graph.event(self.store,current,'reconcile_exhausted',{'error_code':str(code)[:120] or type(exc).__name__})
                        except Exception as state_error:emit('workflow.reconcile_failure_state_failed','ERROR',exc=state_error,run_id=identifier)
                        emit('workflow.reconcile_failed','ERROR',exc=exc,run_id=identifier,task_id=run['id'] if run else None)
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
