"""Owned workflow commands and snapshots; mutations are reconciled asynchronously."""
import asyncio
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse,StreamingResponse
from pydantic import Field,model_validator
from . import workflow_graph as graph,agent_store
from .api_execution import lane,APICapacityError


class ModelChoice(graph.Contract):
    profile:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    reasoning_effort:str=Field(min_length=1,max_length=16)
    profile_digest:str=Field(pattern=r'^[a-f0-9]{64}$')


class Start(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    confirmed:Literal[True]
    authorization:graph.Authorization
    parent_run_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    model_choice:ModelChoice|None=None


class Reuse(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    file:str=Field(min_length=1,max_length=500)
    revision:int=Field(ge=0)
    name:str=Field(min_length=1,max_length=100)


class Command(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    action:Literal['pause','resume','cancel','answer']
    payload:dict=Field(default_factory=dict)

    @model_validator(mode='after')
    def decision_contract(self):
        if self.action=='answer':self.payload=graph.DecisionAnswer.model_validate(self.payload).model_dump(exclude_none=True)
        return self


def public(store,run):
    from .role_display import item_name
    from .execution_locations import for_run
    locations=for_run(store,run)

    value={k:run[k] for k in ('id','project_id','session_id','parent_run_id','scope_confirmed','goal','status','graph_version','sealed','authorization',
                             'authorization_version','replan_after','repair_context','created_at','updated_at','terminal_reason','delivery','delivered_at','input_sequence','input_barrier_sequence')}
    value['state']=value.pop('status')
    now=store.now()
    heartbeat=store.query('''SELECT heartbeat,draining FROM workflow_orchestrators
        WHERE draining=0 ORDER BY heartbeat DESC LIMIT 1''')
    latest=heartbeat[0]['heartbeat'] if heartbeat else None
    value['control_plane']={
        'available':bool(latest is not None and now-latest<15),
        'last_heartbeat_at':latest,
        'stale_seconds':None if latest is None else max(0,now-latest),
        'reconcile_overdue_seconds':max(0,now-run.get('next_reconcile_at',now)),
    }
    # Dynamic pages need authoritative attempt state to settle activity rows
    # when a worker disappears before it can publish a completion event.
    value['activity_attempts']=store.query(
        'SELECT id,status,generation,step_id,task_revision,started,finished FROM execution_attempts WHERE run_id=? ORDER BY generation DESC LIMIT 1000',
        (run['id'],))
    # Offsets refer to the exact joined public transcript (JS UTF-16 units).
    # Never guess the owner of a streamed paragraph from its role or timestamp.
    value['agent_text_spans']=[]
    offset=0
    for message in store.query('SELECT text,attempt FROM agent_messages WHERE task_id=? ORDER BY generation,position',(run['id'],)):
        length=len(message['text'].encode('utf-16-le'))//2
        value['agent_text_spans'].append({'start':offset,'end':offset+length,'attempt_id':message['attempt']})
        offset+=length+2
    from .model_selection import for_run
    value['model_configuration']=for_run(store,run['id'])
    successors=store.query('SELECT id FROM runs WHERE parent_run_id=? ORDER BY created_at DESC LIMIT 1',(run['id'],))
    value['successor_run_id']=successors[0]['id'] if successors else None
    value['activity']=run['activity']
    terminal_notice=''
    if run['status'] in ('FAILED','CANCELLED'):
        reason=run.get('terminal_reason') or ''
        if reason=='deadline_or_budget':
            limit=store.query('SELECT termination_reason FROM execution_limits WHERE task_id=?',(run['id'],))
            cause=limit[0]['termination_reason'] if limit else ''
            terminal_notice={'budget':'任务预算已耗尽，系统停止了后续执行。','deadline':'任务时限已到，系统停止了后续执行。','security':'安全策略触发，系统停止了后续执行。'}.get(cause,'任务预算或时限已耗尽，系统停止了后续执行。')
        else:
            terminal_notice={'authorization_rejected':'当前执行方向未获授权，任务已停止。','planning_budget_exhausted':'规划轮次已耗尽，任务未继续执行。','user_cancelled':'你已取消本次任务，已完成成果会保留。','successor_confirmed':'本轮已由新一轮任务取代，请查看后继任务。'}.get(reason,'任务未完成，请查看下方诊断信息。' if run['status']=='FAILED' else '任务已取消。')
            from .workflow_error_catalog import CATALOG,get as failure_definition
            if reason in CATALOG:terminal_notice=failure_definition(reason).message
    recovery_notices=[]
    records=store.query('SELECT record FROM workflow_diagnostics WHERE workflow=? ORDER BY created DESC LIMIT 50',(run['id'],))
    if any((record:=json.loads(row['record'])).get('error_type') in ('ConnectionError','RemoteDisconnected','ConnectionResetError')
           or record.get('code')=='agent_model_transport_failed' for row in records):
        recovery_notices.append({'code':'model_transport_interrupted','message':'模型连接曾中断，未取得可提交结果；系统没有把该次调用当作成功。'})
    interrupted=store.query("SELECT COUNT(*) AS n FROM execution_attempts WHERE run_id=? AND status IN ('expired','interrupted')",(run['id'],))[0]['n']
    if interrupted:
        recovery_notices.append({'code':'execution_interrupted','message':f'执行进程曾中断 {interrupted} 次；旧 Lease 已失效，未确认模型用量按未知上限保留。'})
    rebound=store.query("SELECT COUNT(*) AS n FROM planner_runs WHERE run_id=? AND error_code='policy_rebound'",(run['id'],))[0]['n']
    if rebound:
        recovery_notices.append({'code':'policy_rebound','message':f'部署后已自动创建 {rebound} 个绑定当前 Agent 策略的新规划任务。'})
    value['recovery_notices']=recovery_notices
    if terminal_notice and recovery_notices:
        terminal_notice+=' 过程记录：'+'；'.join(item['message'].rstrip('。；') for item in recovery_notices)+'。'
    value['terminal_notice']=terminal_notice
    revision_versions={(row['task_id'],row['revision']):row['created_graph_version'] for row in
        store.query('SELECT task_id,revision,created_graph_version FROM workflow_task_revisions WHERE run_id=?',(run['id'],))}
    value['tasks']=[]
    for task in run['tasks']:
        public_task={k:task[k] for k in ('id','type','spec','status','selected','phase','revision','authorization_version','repair_count','recovery_count','infrastructure_interruptions','user_revision_count','attempts',
            'wait_reason','wait_recorded_at','next_run_at','candidate_result','accepted_result','quality','error_code','error_detail','created_at','updated_at')}
        public_task['selected']=bool(task['selected'])
        public_task['revision_graph_version']=revision_versions.get((task['id'],task['revision']),task['created_graph_version'])
        public_task.update(locations[task['id']])
        public_task['display_name']=item_name(task)
        value['tasks'].append(public_task)
    resource_waits={row['step_id']:row['n'] for row in store.query(
        "SELECT step_id,COUNT(*) AS n FROM execution_attempts WHERE run_id=? AND status='resource_wait' GROUP BY step_id",(run['id'],))}
    for task in value['tasks']:task['resource_wait_count']=resource_waits.get(task['id'],0)
    value['failure_details']=[]
    for task in [*run['tasks'],*run['control_jobs']]:
        if task.get('error_code'):
            records=[json.loads(row['record']) for row in store.query('SELECT record FROM workflow_diagnostics WHERE workflow=? AND step=? ORDER BY created DESC LIMIT 3',(run['id'],task['id']))]
            detail=records[0] if records else {}
            errors=list(dict.fromkeys((str(item.get('path',''))+': '+str(item.get('message','')))[:1000]
                for record in records for item in record.get('validation_errors',[]) if isinstance(item,dict)))[:6]
            message=task.get('error_detail') or detail.get('message','')
            from .workflow_error_catalog import public_metadata
            value['failure_details'].append({'task_id':task['id'],'name':item_name(task),'code':task['error_code'],
                'message':'；'.join([text for text in [message,*errors] if text]),'error_type':detail.get('error_type',''),
                'classification':detail.get('classification',''),**public_metadata(task['error_code'])})
    value['control_jobs']=[]
    for job in run['control_jobs']:
        public_job={key:job[key] for key in ('id','control_kind','status','phase','attempts','error_code','error_detail','created_graph_version','created_at','updated_at')}
        public_job.update(locations[job['id']])
        public_job['display_name']=item_name(job)
        value['control_jobs'].append(public_job)
    from .run_inputs import public as public_input
    value['inputs']=[public_input(row) for row in store.query('SELECT * FROM run_inputs WHERE run_id=? ORDER BY sequence',(run['id'],))]
    replies={row['id'][8:]:row['content'] for row in store.query("SELECT id,content FROM session_messages WHERE run_id=? AND id LIKE 'message:%'",(run['id'],))}
    reply_times={job['spec']['message_input_id']:json.loads(job['snapshot_json']).get('snapshot_at')
                 for job in run['control_jobs'] if job['control_kind']=='MESSAGE'}
    for item in value['inputs']:
        item['reply']=replies.get(item['id'],'')
        item['reply_snapshot_at']=reply_times.get(item['id'])
    value['message_pending']=sum(item['status']=='ROUTING' for item in value['inputs'])
    value['drain_set']=run['drain_set']
    value['dependencies']=run['dependencies']
    value['replacements']=store.query('SELECT old_task_id,new_task_id,graph_version,source FROM run_task_replacements WHERE run_id=? ORDER BY graph_version,old_task_id',(run['id'],))
    for target,source in zip(value['tasks'],run['tasks']):
        if source['profile']:
            target['agent_policy']={key:source['profile'][key] for key in ('id','version','harness','model_policy','output_schema','artifact_policy')}
            target['agent_policy']['digest']=graph.digest(source['profile'])
    value['planners']=store.query('SELECT id,task_id,base_graph_version,status,applied_graph_version,trigger_json,error_code,created_at,updated_at FROM planner_runs WHERE run_id=? ORDER BY created_at',(run['id'],))
    for planner in value['planners']:
        trigger=json.loads(planner.pop('trigger_json'))
        task_ids=[]
        if isinstance(trigger.get('completed'),list):task_ids.extend(item for item in trigger['completed'] if isinstance(item,str))
        if isinstance(trigger.get('task_id'),str) and trigger['task_id']:task_ids.append(trigger['task_id'])
        control_ids=[]
        for key in ('rejected','review_incomplete','failure_diagnosed','review_rejected','task_review_rejected'):
            if isinstance(trigger.get(key),str) and trigger[key]:control_ids.append(trigger[key])
        planner['trigger_task_ids']=list(dict.fromkeys(task_ids))
        planner['trigger_control_job_ids']=list(dict.fromkeys(control_ids))
    value['plan_drafts']=store.query('''SELECT planning_request_id,schema_version,status,revision,
        repeated_error_count,created_at,updated_at FROM workflow_plan_drafts WHERE run_id=? ORDER BY created_at''',(run['id'],))
    value['decisions']=store.query("SELECT id,kind,graph_version,authorization_version,snapshot_digest,request_json,status FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))
    for item in value['decisions']:
        item['request']=json.loads(item.pop('request_json'))
        if item['kind']=='external_submission' and 'external_id_queryable' not in item['request']:
            task=graph.work_item(run,item['request'].get('task_id',''))
            if task and task['type']=='operation':
                try:
                    from .operation_lifecycle import for_operation
                    from .operations import get as operation
                    queryable='poll' in for_operation(operation(task['spec']['operation'],task['spec']['implementation'])).protocol.phases
                except (KeyError,ValueError):queryable=False
                item['request']['external_id_queryable']=queryable
                item['request']['resubmit_may_duplicate']=True
                if not queryable:
                    item['request']['question']='同步外部请求的回执丢失，且该接口不提供可查询的任务 ID。系统无法确认服务端是否已处理；重新提交可能重复生成或重复计费。请明确确认是否仍要重新提交。'
                    item['request']['options']=['resubmit']
    question_cutoff=max((item['request'].get('input_through_sequence',0) for item in value['decisions'] if item['kind']=='input_question'),default=0)
    for item in value['inputs']:
        item['waiting_for_answer']=bool(item['status']=='ACCEPTED' and item['sequence']<=question_cutoff)
    answers=store.query("SELECT content FROM session_messages WHERE run_id=? AND role='assistant' AND id LIKE 'answer:%' ORDER BY created_at DESC LIMIT 1",(run['id'],))
    value['conversation_answer']=answers[0]['content'] if answers else ''
    blocked=store.query("SELECT payload_json,created_at FROM workflow_graph_events WHERE run_id=? AND kind='delivery_blocked' ORDER BY seq DESC LIMIT 1",(run['id'],))
    if run['status']=='WAITING_USER' and run['terminal_reason']=='delivery_targets_incomplete':
        details=json.loads(blocked[0]['payload_json']) if blocked else {}
        scope_decision=next((item['id'] for item in value['decisions'] if item['kind']=='delivery_scope_change'),None)
        missing=[]
        if details.get('missing_names'):missing.append('文件：'+'、'.join(details['missing_names'][:8]))
        if details.get('missing_extensions'):missing.append('格式：'+'、'.join(str(x).upper() for x in details['missing_extensions'][:8]))
        if details.get('missing_roles'):missing.append('输出角色：'+'、'.join(details['missing_roles'][:8]))
        if details.get('missing_task_ids'):missing.append('交付任务：'+'、'.join(details['missing_task_ids'][:8]))
        missing_text='；'.join(missing) or '已确认的交付项目'
        value['blocking_action']={
            'kind':'delivery_scope_change' if scope_decision else 'delivery_error',
            'code':'delivery_targets_incomplete',
            'title':'交付范围需要确认' if scope_decision else '交付选择不完整',
            'message':('当前成果未满足冻结的全部交付范围。请确认保留哪些交付内容，或补充缺少的产物。' if scope_decision else
                       '最终交付选择不完整（'+missing_text+'），尚未发布。这是交付检查问题，无需缩小需求；可以要求按原范围重新检查或补齐缺少的文件。'),
            'current_targets':run['authorization'].get('delivery_targets',[]),
            'missing_task_ids':details.get('missing_task_ids',[]),
            'missing_extensions':details.get('missing_extensions',[]),
            'missing_names':details.get('missing_names',[]),
            'missing_roles':details.get('missing_roles',[]),
            'decision_id':scope_decision,
            **({} if scope_decision else {'action':'replan'}),
            'created_at':blocked[0]['created_at'] if blocked else run['updated_at'],
        }
    elif run['status']=='WAITING_USER' and run['terminal_reason']:
        if run['terminal_reason']=='execution_failed':
            failed=[item for item in value['failure_details'] if item['code'] in ('execution_failed','agent_failed')]
            first=failed[0] if failed else {}
            message=first.get('message') or ''
            if first.get('error_type')=='ImportError':
                message='系统执行错误：OpenSandbox 运行时加载失败（ImportError）。这不是输入问题；修复运行时后可点击“继续”重试失败步骤。'
            value['blocking_action']={'kind':'execution_failure','code':'execution_failed','title':'系统执行失败',
                'message':message or '系统执行失败，未能完成当前步骤。请查看失败详情并点击“继续”重试；这不需要补充素材。',
                'action':'retry','details':failed}
            if first.get('classification')=='parameters':
                value['blocking_action'].update(title='执行方案参数需要修订',action='replan',
                    message=message or '参数与操作契约不匹配，请在聊天框说明修订要求，重新规划后执行。')
        else:
            waiting_guidance={
                'clarification_exhausted':('已停止重复追问','系统连续澄清仍未确定下一步，已停止自动追问，也没有重新处理素材。上一轮成果仍可使用；你可以提出明确的修改要求，或停止本轮。'),
                'user_rejected':('当前方向已停止','已按你的选择停止这个方向，不会自动重新规划；已有成果仍会保留。'),
                'planning_budget_exhausted':('本轮规划轮次已耗尽','请停止本轮并新建一轮；如仍需多轮规划，请提高规划轮次上限。'),
                'external_wait_timeout':('外部任务等待超时','上游任务等待已超过上限。请在下方选择继续等待，或放弃该任务并重新规划；已有成果会保留。'),
                'storage_quota_exceeded':('存储额度不足','请清理旧项目或增加存储额度，然后点击“继续”重新检查发布。'),
                'task_requires_resolution':('任务需要处理','请提交补充要求，系统会在当前执行安全收尾后重新规划；也可以停止本轮。“继续”仅尝试恢复可安全重试的步骤，不会自动重新规划。'),
                'workflow_stalled':('工作流无法继续推进','系统检测到本轮没有任何可运行步骤、活动执行、等待中的外部任务或待确认决策，已停止空转。无需猜测补充信息；请查看任务状态和错误详情后重新规划，或停止本轮。'),
            }
            pending=value['decisions'][0] if value['decisions'] else None
            request=(pending or {}).get('request') or {}
            from .workflow_error_catalog import CATALOG,get as failure_definition,public_metadata
            if run['terminal_reason'] in waiting_guidance:
                title,message=waiting_guidance[run['terminal_reason']]
            elif run['terminal_reason'] in CATALOG:
                definition=failure_definition(run['terminal_reason']);title,message=definition.title,definition.message
            else:
                title,message='需要你的处理',request.get('question') or request.get('message') or '系统未登记该错误类型；请查看下方真实错误详情，不需要猜测应补充什么信息。'
            first=next((item for item in value['failure_details'] if item['code']==run['terminal_reason']),{})
            if not pending and first.get('message'):message=first['message']
            value['blocking_action']={'kind':'workflow_action','code':run['terminal_reason'],
                'title':title,'message':message,'decision_id':pending['id'] if pending else None,
                'details':value['failure_details'] if not pending else [],**public_metadata(run['terminal_reason']),
                'required_fields':request.get('required_fields',[]),'examples':request.get('examples',[])}
            if run['terminal_reason']=='review_incomplete' and not pending:
                latest=store.query("SELECT task_id,subject_task,purpose,decision_json FROM workflow_reviews WHERE run_id=? AND purpose IN ('task','final') AND status='rejected' ORDER BY updated_at DESC,created_at DESC LIMIT 1",(run['id'],))
                decision=json.loads(latest[0]['decision_json']) if latest else {}
                feedback=str(decision.get('feedback') or '').strip()
                value['blocking_action'].update(action='replan',
                    message=(feedback+'\n自动补证已到上限，现有成果尚未正式交付。你可以说明修改要求后重新规划，或停止本轮。') if feedback else
                            '审查未返回可用的详细意见，系统已暂停且未交付。可停止本轮；诊断中保留了审查记录。')
                if feedback:value['blocking_action']['details']=[{'task_id':latest[0]['subject_task'] or latest[0]['task_id'],
                    'name':'最终审查' if latest[0]['purpose']=='final' else '步骤审查','code':decision.get('verdict','inconclusive'),'message':feedback}]
    if run['status']=='WAITING_USER' and value.get('blocking_action') and not value['blocking_action'].get('decision_id'):
        from .workflow_messages import retry_options
        options=retry_options(store,run)
        if options:value['blocking_action'].update(action='retry',retry_options=options)
    value['reviews']=store.query('SELECT id,task_id,purpose,subject_task,graph_version,status FROM workflow_reviews WHERE run_id=? ORDER BY created_at',(run['id'],))
    value['commands']=store.query('SELECT id,action,status,error_code,created_at,applied_at FROM workflow_commands WHERE run_id=? ORDER BY created_at DESC LIMIT 20',(run['id'],))
    value['diagnoses']=[{'task_id':row['subject_task'],'diagnosis':json.loads(row['decision_json'])} for row in
        store.query("SELECT subject_task,decision_json FROM workflow_reviews WHERE run_id=? AND purpose='diagnose' AND status='completed' ORDER BY created_at",(run['id'],))]
    value['external_cleanup']=store.query('SELECT state,phase,error FROM execution_external_cleanup WHERE task_id=?',(run['id'],))
    value['token_usage']=store.query('''SELECT
        COALESCE(SUM(input_settled_tokens),0) AS settled_input,
        COALESCE(SUM(output_settled_tokens),0) AS settled_output,
        COALESCE(SUM(CASE WHEN state IN ('partially_settled_output_unknown','interrupted_output_unknown') THEN output_reserved_tokens ELSE 0 END),0) AS unknown_output_reserved,
        COALESCE(SUM(CASE WHEN used_tokens IS NULL OR state='interrupted_usage_unknown' THEN reserved_tokens ELSE 0 END),0) AS fully_unknown_reserved,
        COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS committed
        FROM workflow_model_reservations WHERE run_id=?''',(run['id'],))[0]
    value['token_usage']['known']=value['token_usage']['settled_input']+value['token_usage']['settled_output']
    value['token_usage']['reserved_or_unknown']=value['token_usage']['unknown_output_reserved']+value['token_usage']['fully_unknown_reserved']
    committed=value['token_usage']['committed'];value['token_usage']['limit']=run['authorization']['model_token_budget']
    value['token_usage']['remaining']=max(0,value['token_usage']['limit']-committed)
    value['cleanup_pending']=sum(1 for item in value['external_cleanup'] if item['state']!='complete')+store.query("SELECT COUNT(*) AS n FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],))[0]['n']
    def delivery_urls(identifier,artifacts):
        from urllib.parse import quote
        return [{**artifact,'url':f'api/projects/{run["project_id"]}/runs/{identifier}/files/{quote(artifact["file"],safe="")}'} for artifact in artifacts]
    value['delivery']=delivery_urls(run['id'],value['delivery'])
    value['parent_result']=None
    ancestor=run['parent_run_id'];seen={run['id']}
    while ancestor and ancestor not in seen and len(seen)<=8:
        seen.add(ancestor)
        parents=store.query('SELECT id,parent_run_id,status,goal,delivery_json FROM runs WHERE id=? AND project_id=?',
                            (ancestor,run['project_id']))
        if not parents:break
        parent=parents[0]
        if json.loads(parent['delivery_json']):
            value['parent_result']={'id':parent['id'],'state':parent['status'],'goal':parent['goal'],
                                    'delivery':delivery_urls(parent['id'],json.loads(parent['delivery_json']))}
            break
        ancestor=parent['parent_run_id']
    return value


def register(app,store,own_project):
    store.execute('''CREATE TABLE IF NOT EXISTS workflow_start_requests(
        id TEXT PRIMARY KEY,project_id TEXT NOT NULL,owner TEXT NOT NULL,digest TEXT NOT NULL,run_id TEXT NOT NULL)''')

    def own_run(project,identifier,request):
        own_project(project,request);run=graph.get(store,identifier)
        if not run or run['project_id']!=project:raise HTTPException(404,'工作流不存在或不属于当前项目。')
        request.state.workflow_observation={'task_id':run['id'],'run_id':run['id']}
        return run

    @app.get('/api/projects/{project_id}/runs/capabilities')
    def capabilities(project_id:str,request:Request):
        own_project(project_id,request)
        from .specialist_profiles import catalogue
        workers=store.query('''SELECT c.worker FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker
            WHERE w.draining=0 AND w.heartbeat>?''',(store.now()-30,))
        from .workflow_planning import available_operations
        try:operations=available_operations(store) or []
        except PermissionError:operations=[]
        from .model_selection import options
        from .task_leases import daily_usage
        owner=store.query('SELECT owner FROM projects WHERE id=?',(project_id,))[0]['owner']
        return {'operations':operations,'profiles':catalogue(),'workers_ready':bool(workers),
                'daily_quota':daily_usage(store,owner,'model'),
                'model_choices':options(store,project_id),
                'model_protocol_policy':'plan_draft_v1_strict_preferred',
                'content_fingerprint':graph.content_fingerprint(store,project_id),
                'authorization_schema':graph.Authorization.model_json_schema()}

    @app.get('/api/projects/{project_id}/runs')
    def list_runs(project_id:str,request:Request):
        own_project(project_id,request)
        return {'runs':store.query('''SELECT id,session_id,parent_run_id,goal,status AS state,graph_version,sealed,created_at,updated_at
            FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 50''',(project_id,))}

    @app.post('/api/projects/{project_id}/runs',status_code=202)
    @lane('write')
    def start(project_id:str,body:Start,request:Request):
        own_project(project_id,request);owner=request.state.owner
        raw={**body.authorization.model_dump(),'require_plan_confirmation':True};identity=graph.digest({'authorization':raw,'parent_run_id':body.parent_run_id,
            'model_choice':body.model_choice.model_dump() if body.model_choice else None})
        try:
            with store.connect() as db:
                previous=db.execute('SELECT * FROM workflow_start_requests WHERE id=?',(body.request_id,)).fetchone()
                if previous:
                    if (previous['owner'],previous['project_id'],previous['digest'])!=(owner,project_id,identity):raise ValueError('请求标识已用于其他内容')
                    return public(store,graph.get(store,previous['run_id']))
                from .task_leases import daily_usage
                quota=daily_usage(store,owner,'model')
                if quota['blocked']:raise HTTPException(409,f"今日模型调用额度已用完（{quota['used']}/{quota['limit']}），请等待恢复后再新建任务。")
                parent=own_run(project_id,body.parent_run_id,request) if body.parent_run_id else None
                from .model_selection import resolve,resolve_roles
                configuration=resolve(body.model_choice) if body.model_choice else None
                roles=resolve_roles(configuration)
                identifier=uuid.uuid4().hex
                run=graph.create(store,project_id,identifier,raw,parent_run_id=parent['id'] if parent else None,
                                 model_configuration=configuration,role_model_configurations=roles)
                request.state.workflow_observation={'run_id':run['id']}
                db.execute('INSERT INTO workflow_start_requests VALUES (?,?,?,?,?)',(body.request_id,project_id,owner,identity,run['id']))
                return public(store,run)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/projects/{project_id}/runs/{run_id}')
    def detail(project_id:str,run_id:str,request:Request):
        # Run state, decisions, inputs and tasks must describe one moment.
        # This MVCC read does not take the control-plane mutation lock.
        with store.connect(readonly=True,snapshot=True):
            return public(store,own_run(project_id,run_id,request))

    @app.get('/api/projects/{project_id}/runs/{run_id}/files/{filename}')
    async def delivery_file(project_id:str,run_id:str,filename:str,request:Request):
        def resolve():
            run=own_run(project_id,run_id,request)
            item=next((item for item in run['delivery'] if item['file']==filename),None)
            if run['delivered_at'] is None or item is None:raise HTTPException(404,'文件不属于已发布成果。')
            return item
        item=await app.state.api_execution.run('read',resolve)
        location=await app.state.api_execution.run('storage',store.artifacts.materialize,project_id,item['artifact_id'])
        active=item['type'] in ('text/html','image/svg+xml','application/xhtml+xml','application/xml','text/xml')
        return FileResponse(location,media_type=item['type'],filename=item['name'],
            content_disposition_type='attachment' if active else 'inline',
            headers={'Content-Security-Policy':"sandbox; default-src 'none'"} if active else {})

    @app.get('/api/projects/{project_id}/runs/{run_id}/tasks/{task_id}/file')
    async def task_file(project_id:str,run_id:str,task_id:str,file:str,request:Request):
        from pathlib import Path
        import mimetypes
        def resolve():
            run=own_run(project_id,run_id,request)
            task=next((t for t in run['tasks'] if t['id']==task_id and not t.get('control_kind')),None)
            if not task or file not in task['candidate_result']+task['accepted_result']:raise HTTPException(404,'产物不属于当前步骤。')
            rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                             (run['id'],task_id,task['revision']))
            record=json.loads(rows[0]['manifest']).get(file) if rows else None
            if not record:raise HTTPException(404,'产物尚未持久化。')
            return record
        record=await app.state.api_execution.run('read',resolve)
        location=await app.state.api_execution.run('storage',store.artifacts.materialize,project_id,record['artifact'])
        media_type=mimetypes.guess_type(file)[0] or 'application/octet-stream'
        active=media_type in ('text/html','image/svg+xml','application/xhtml+xml','application/xml','text/xml')
        return FileResponse(location,media_type=media_type,filename=Path(file).name,
                            content_disposition_type='attachment' if active else 'inline',
                            headers={'Content-Security-Policy':"sandbox; default-src 'none'"} if active else {})

    @app.post('/api/projects/{project_id}/runs/{run_id}/tasks/{task_id}/reuse',status_code=201)
    @lane('write')
    def reuse(project_id:str,run_id:str,task_id:str,body:Reuse,request:Request):
        from pathlib import PurePosixPath
        import uuid
        try:
            with store.connect() as db:
                run=own_run(project_id,run_id,request)
                previous=db.execute('SELECT * FROM workflow_asset_reuse WHERE id=?',(body.request_id,)).fetchone()
                if previous:
                    if (previous['project_id'],previous['run_id'],previous['task_id'],previous['revision'],previous['path'])!=(project_id,run['id'],task_id,body.revision,body.file):
                        raise ValueError('Reuse request identity changed')
                    return next(a for a in store.assets(project_id) if a['id']==previous['asset_id'])
                task=next((t for t in run['tasks'] if t['id']==task_id and not t.get('control_kind')),None)
                if not task or task['status']!='succeeded' or task['revision']!=body.revision or body.file not in task['accepted_result']:
                    raise ValueError('Only the current accepted task revision can be reused')
                if not task['quality'].get('technical_valid'):raise ValueError('Source has not passed technical acceptance')
                rows=db.execute('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',(run['id'],task_id,body.revision)).fetchone()
                record=json.loads(rows['manifest']).get(body.file) if rows else None
                if not record or not record.get('artifact'):raise ValueError('Source lacks an immutable object')
                artifact=store.artifacts.record(project_id,record['artifact'])
                if artifact['sha256']!=record['sha256'] or artifact['size']!=record['size']:raise ValueError('Source hash changed')
                checked=next((f for f in task['quality'].get('files',[]) if f['name']==body.file),None)
                if not checked or checked.get('sha256')!=record['sha256']:raise ValueError('Source does not match reviewed bytes')
                if len(store.assets(project_id))>=50:raise ValueError('Project asset limit reached')
                asset_id=uuid.uuid4().hex;filename=asset_id+PurePosixPath(body.file).suffix
                metadata={k:v for k,v in checked.items() if k not in ('name','sha256')}
                if checked.get('video'):metadata['kind']='video'
                elif checked.get('audio_track_present') and not checked.get('video'):metadata['kind']='audio'
                if metadata.get('kind') not in ('image','video','audio','subtitle','json','file'):raise ValueError('Output type is not importable')
                if metadata.get('audio_tracks'):
                    metadata['audio_tracks']=[{'index':i,'codec':track.get('codec_name',''),
                        'channels':track.get('channels',0),'title':f'音轨 {i}'} for i,track in enumerate(metadata['audio_tracks'])]
                metadata['size']=record['size']
                metadata['provenance']={'run_id':run['id'],'task_id':task_id,'revision':body.revision,'sha256':record['sha256']}
                store.check_storage(project_id,record['size'])
                store.add_asset(project_id,asset_id,body.name,filename,metadata,artifact=artifact)
                db.execute('INSERT INTO workflow_asset_reuse VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (body.request_id,project_id,run['id'],task_id,body.revision,body.file,artifact['id'],artifact['sha256'],asset_id,store.now()))
                graph.event(store,run,'accepted_asset_reused',{'task_id':task_id,'revision':body.revision})
                return next(a for a in store.assets(project_id) if a['id']==asset_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    from .run_inputs import Input,accept as accept_input

    @app.post('/api/projects/{project_id}/runs/{run_id}/inputs',status_code=202)
    @lane('critical')
    def input_message(project_id:str,run_id:str,body:Input,request:Request):
        own_run(project_id,run_id,request)
        if body.source!='user':raise HTTPException(422,'质量反馈必须绑定原决策。')
        try:return accept_input(store,run_id,request.state.owner,body.model_dump())
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/projects/{project_id}/runs/{run_id}/events')
    def events(project_id:str,run_id:str,request:Request,after:int=0,limit:int=100):
        run=own_run(project_id,run_id,request)
        records=store.query('SELECT seq,kind,graph_version,payload_json,created_at FROM workflow_graph_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                            (run['id'],max(0,after),max(1,min(200,limit))))
        for record in records:record['payload']=json.loads(record.pop('payload_json'))
        return {'events':records,'cursor':records[-1]['seq'] if records else after}

    @app.get('/api/projects/{project_id}/runs/{run_id}/stream',response_class=StreamingResponse)
    async def stream(project_id:str,run_id:str,request:Request,after:int=0):
        from .agent_stream import AsyncStreamReader,bus as stream_bus
        from .public_messages import read as public_messages
        api_execution=app.state.api_execution
        permit=await api_execution.streams.acquire()
        try:await api_execution.run('auth',own_run,project_id,run_id,request)
        except BaseException:
            permit.release();raise
        reader=AsyncStreamReader(stream_bus());header=request.headers.get('last-event-id','').strip()
        try:db_position=max(0,int(header or after))
        except ValueError:db_position=max(0,after)
        public_kinds=('progress','work','tool','workflow_graph','note')

        def snapshot(cursor,with_events=True,with_text=True):
            rows=store.query('SELECT status,updated_at FROM runs WHERE id=?',(run_id,))
            marks=','.join('?' for _ in public_kinds)
            events=store.query(f'SELECT seq,kind,payload,created FROM agent_events WHERE run_id=? AND kind IN ({marks}) AND seq>? ORDER BY seq LIMIT 200',
                (run_id,*public_kinds,cursor)) if with_events else []
            active={row['attempt'] for row in store.query(
                'SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run_id,store.now()))}
            return (rows[0] if rows else None),events,public_messages(store,run_id) if with_text else [],active

        def durable_text(messages,active_attempts):
            if not messages:return None
            latest=max(messages,key=lambda item:(item['generation'],item['sequence']))
            return {'text':'\n\n'.join(item['text'] for item in messages if item['text']),
                'text_version':[latest['generation'],latest['sequence']],
                'text_phase':'streaming' if any(item['status']=='streaming' and item['attempt'] in active_attempts
                    for item in messages if item['generation']==latest['generation']) else 'idle',
                'published_at':latest['updated']}

        async def generate():
            nonlocal db_position
            redis_position=header if '-' in header else await reader.latest_id(run_id)
            redis_mode=redis_position is not None;redis_position=redis_position or '0-0'
            replay_events=not header or '-' not in header;initial=True;heartbeat=time.monotonic();last_snapshot=0.;status=''
            text_signature=None
            while not await request.is_disconnected():
                if initial or replay_events or not redis_mode or time.monotonic()-last_snapshot>=2:
                    try:run,records,messages,active_attempts=await api_execution.run('stream_read',snapshot,db_position,replay_events or not redis_mode,initial or not redis_mode)
                    except APICapacityError:
                        yield 'retry: 2000\nevent: retry\ndata: {"code":"API_STREAM_BUSY"}\n\n';return
                    if not run:yield 'event: gone\ndata: {}\n\n';return
                    status=run['status'];last_snapshot=time.monotonic();text=durable_text(messages,active_attempts)
                    signature=json.dumps(text,sort_keys=True) if text else None
                    if text and signature!=text_signature:
                        text_signature=signature;yield 'event: text_snapshot\ndata: '+json.dumps(text,ensure_ascii=False)+'\n\n'
                    for record in records:
                        db_position=record['seq'];record['payload']=json.loads(record['payload'])
                        event='agent_text' if record['kind']=='note' else 'activity'
                        yield f'id: {db_position}\nevent: {event}\ndata: '+json.dumps(record,ensure_ascii=False)+'\n\n'
                    # In Redis mode the stream starts at the current tail, so
                    # every durable database page must be replayed before we
                    # switch to Redis increments. Otherwise a reopened page
                    # permanently loses all history after the first 200 rows.
                    if redis_mode and replay_events and records:
                        initial=False
                        continue
                    initial=False;replay_events=False
                if redis_mode:
                    live=await reader.read(run_id,redis_position,200,200)
                    if live is None:redis_mode=False;continue
                    for record in live:
                        redis_position=record['id'];kind=record['kind']
                        if kind=='text_snapshot':yield f'id: {redis_position}\nevent: text_snapshot\ndata: '+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                        elif kind=='text_delta':yield f'id: {redis_position}\nevent: text\ndata: '+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                        elif kind in public_kinds:
                            event='agent_text' if kind=='note' else 'activity';value={'seq':redis_position,'kind':kind,'payload':record['payload'],'created':int(redis_position.split('-',1)[0])/1000}
                            yield f'id: {redis_position}\nevent: {event}\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n'
                    if status in ('SUCCEEDED','FAILED','CANCELLED') and not live:
                        yield 'event: done\ndata: '+json.dumps({'status':status})+'\n\n';return
                else:
                    if status in ('SUCCEEDED','FAILED','CANCELLED') and not records:
                        yield 'event: done\ndata: '+json.dumps({'status':status})+'\n\n';return
                    await asyncio.sleep(.5)
                if time.monotonic()-heartbeat>12:
                    yield 'event: heartbeat\ndata: '+json.dumps({'time':time.time()})+'\n\n';heartbeat=time.monotonic()

        class ManagedStream(StreamingResponse):
            async def __call__(self,*args,**kwargs):
                try:return await super().__call__(*args,**kwargs)
                finally:
                    import anyio
                    try:
                        with anyio.CancelScope(shield=True):await reader.aclose()
                    finally:permit.release()
        return ManagedStream(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.post('/api/projects/{project_id}/runs/{run_id}/commands',status_code=202)
    @lane('critical')
    def command(project_id:str,run_id:str,body:Command,request:Request):
        run=own_run(project_id,run_id,request)
        try:return graph.command(store,run['id'],request.state.owner,body.action,body.payload,body.request_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
