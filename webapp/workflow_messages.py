"""Manager message decisions precede business-input barriers; no media tools."""
import json
import time
from typing import Literal
from pydantic import Field,model_validator
from . import workflow_graph as graph

INSTRUCTIONS='''You are the existing task Manager, handling one new user message while work may continue.
Choose exactly one available message tool. Do not create a plan, execute media, grant permission, approve a pending decision, or declare the business Run complete.
reply_message: greetings, casual conversation, progress questions, explanation of the current plan, or a precise clarification when intent is ambiguous. This does not pause work or invalidate a confirmation.
retry_current_failure: choose one server-supplied retry option when the user asks to retry the currently reported failure. The host revalidates it against current state; never invent a retry target.
replan_message: an explicit request to change the current task, including a substantive answer to a pending planning question. The host will establish a barrier, drain already-started work, then request a fresh plan. Do not plan from this moving snapshot.
new_task_message: an explicitly independent new task. The host creates a separate unapproved Run; the current task continues. Never use this merely to escape an error or budget.
For a mixed question and change, answer the question and choose replan_message/new_task_message as appropriate. Say clearly what change was received; do not claim it has already executed.
Use only the supplied facts, with snapshot time. Pending changes are not applied changes. A greeting or question is not an approval, scope change, or cancellation. Requests to stop should point to the explicit Stop control, not silently claim cancellation.
Before the tool call emit one short public sentence; the answer field is the actual user-visible reply. Do not reveal credentials or private reasoning.'''


class MessageDecision(graph.Contract):
    action:Literal['reply','retry','replan','new_task']
    answer:str=Field(min_length=1,max_length=4000)
    change:str=Field(default='',max_length=4000)
    retry_option_id:str=Field(default='',max_length=64)

    @model_validator(mode='after')
    def complete(self):
        if not self.answer.strip():raise ValueError('A concrete user-visible answer is required')
        if self.action in ('replan','new_task') and not self.change.strip():raise ValueError('Describe the explicit requested change')
        if self.action=='reply' and self.change:raise ValueError('A reply cannot silently carry a change')
        if self.action=='retry' and (not self.retry_option_id or self.change):raise ValueError('Retry requires one server retry option and no task change')
        if self.action!='retry' and self.retry_option_id:raise ValueError('Only retry may select a retry option')
        return self


class MessageProtocol:
    def __init__(self,retry_options=()):
        from .workflow_model_protocol import obj
        text={'type':'string','minLength':1,'maxLength':4000}
        self.schemas={'reply_message':obj({'answer':text}),
                      'replan_message':obj({'answer':text,'change':text}),
                      'new_task_message':obj({'answer':text,'change':text})}
        self.retry_options={item['id']:item for item in retry_options}
        if self.retry_options:
            self.schemas['retry_current_failure']=obj({'answer':text,'retry_option_id':{
                'type':'string','enum':list(self.retry_options)}})

    def definitions(self):
        from .workflow_model_protocol import schema_for
        descriptions={'reply_message':'Answer or ask a precise clarification without changing the task.',
                      'retry_current_failure':'Retry the currently reported failure using exactly one server-supplied retry option.',
                      'replan_message':'Request safe replanning for an explicit change; do not produce a plan.',
                      'new_task_message':'Start planning an explicitly independent new task, without stopping this task.'}
        return [{'name':name,'description':descriptions[name],'schema':schema_for(schema)} for name,schema in self.schemas.items()]

    def decode(self,name,arguments):
        from .workflow_model_protocol import transform,CommandError
        if name not in self.schemas:raise CommandError('action_unavailable','tool','Only message disposition tools are available')
        payload=transform(arguments,self.schemas[name])
        action={'reply_message':'reply','retry_current_failure':'retry','replan_message':'replan','new_task_message':'new_task'}[name]
        return MessageDecision(action=action,**payload).model_dump()


def retry_options(store,run):
    """Server-owned retry capabilities; natural language never creates these."""
    if run['status']!='WAITING_USER' or run.get('delivered_at') is not None:return []
    if store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)):return []
    if store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],store.now())):return []
    if store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],)):return []
    used=store.query('SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS n FROM workflow_model_reservations WHERE run_id=?',(run['id'],))[0]['n']
    if used>=run['authorization']['model_token_budget']:return []
    rounds=store.query('SELECT COUNT(*) AS n FROM planner_runs WHERE run_id=?',(run['id'],))[0]['n']
    if rounds>=run['authorization']['max_planning_rounds']:return []
    transient={'agent_model_transport_failed','model_rate_limited','model_service_unavailable'}
    plans=[job for job in reversed(run['control_jobs']) if job['control_kind']=='PLAN' and job['status']=='paused'
           and job['error_code'] in transient and job['error_code']==run['terminal_reason']]
    if not plans:return []
    subject=plans[0]
    binding={'kind':'retry_planning','run_id':run['id'],'graph_version':run['graph_version'],
             'authorization_version':run['authorization_version'],'terminal_reason':run['terminal_reason'],
             'subject_id':subject['id'],'subject_revision':subject['revision']}
    return [{**binding,'id':graph.digest(binding),'label':'重试当前规划','description':'按当前冻结任务和授权重新发起规划，不改变任务契约。'}]


def failure_context(run):
    failed=[]
    for item in graph.work_items(run):
        if item['status'] not in ('paused','failed'):continue
        failed.append({'subject_id':item['id'],'control_kind':item.get('control_kind',''),
            'status':item['status'],'code':item.get('error_code',''),'detail':item.get('error_detail','')})
    return {'terminal_reason':run.get('terminal_reason',''),'failed_subjects':failed[-12:]}


def pending(store,run_id):
    return store.query("SELECT * FROM run_inputs WHERE run_id=? AND status='ROUTING' ORDER BY sequence",(run_id,))


def queue(store,run):
    rows=pending(store,run['id'])
    if not rows or run['status'] in ('CANCELLING','FAILED','CANCELLED'):return
    message=rows[0];identifier='message_'+message['id']
    if store.query('SELECT id FROM control_jobs WHERE run_id=? AND id=?',(run['id'],identifier)):return
    from .specialist_profiles import get
    now=store.now()
    options=retry_options(store,run)
    from .conversation_memory import snapshot as memory_snapshot
    frozen={'input_id':message['id'],'input_sequence':message['sequence'],'message':message['content'],
        'attachments':json.loads(message['attachments_json']),'context':json.loads(message['context_json']),
        'snapshot_at':now,'run_id':run['id'],'goal':run['goal'],'state':run['status'],'graph_version':run['graph_version'],
        'authorization_version':run['authorization_version'],'delivery_targets':run['authorization']['delivery_targets'],
        'tasks':[{'id':t['id'],'name':t['spec'].get('name',''),'objective':t['spec'].get('objective','')[:1000],
                  **{key:t.get(key) for key in ('status','revision','error_code','error_detail')}} for t in graph.active_tasks(run)[:64]],
        'task_count':len(graph.active_tasks(run)),'tasks_truncated':len(graph.active_tasks(run))>64,
        'decisions':[dict(row,request=json.loads(row['request_json'])) for row in store.query(
            "SELECT id,kind,request_json FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))],
        'delivery':[{key:item.get(key) for key in ('name','type','size')} for item in run['delivery']],
        'current_failure':failure_context(run),'retry_options':options,
        'conversation_memory':memory_snapshot(store,run)}
    spec={'profile':'media_planner','objective':message['content'],'message_input_id':message['id']}
    store.execute('''INSERT INTO control_jobs(run_id,id,type,authorization_version,control_kind,snapshot_json,snapshot_digest,
        spec_json,profile_json,status,created_graph_version,created_at,updated_at)
        VALUES (?,?,'agent',?,'MESSAGE',?,?,?,?,'ready',?,?,?)''',
        (run['id'],identifier,run['authorization_version'],graph.encode(frozen),graph.digest(frozen),graph.encode(spec),
         graph.encode(get('media_planner').snapshot()),run['graph_version'],now,now))
    graph.event(store,run,'message_routing',{'input_id':message['id'],'task_id':identifier})


def execute(host,lease,run,task,profile):
    from .workflow_agents import model,AgentReplyError
    from .workflow_dispatch import stage_result
    frozen=json.loads(task['snapshot_json']);protocol=MessageProtocol(frozen.get('retry_options',[]));prompt=graph.encode(frozen)
    deadline=time.monotonic()+90;transport={'strict':True}
    for _ in range(3):
        lease.check()
        try:
            decision=MessageDecision.model_validate(model(host,lease,run,profile,prompt,[],deadline=deadline,protocol=protocol,transport=transport))
            return stage_result(host.store,lease,'succeeded',{'message_decision':decision.model_dump(),
                'input_id':frozen['input_id'],'snapshot_digest':task['snapshot_digest']})
        except (AgentReplyError,ValueError) as exc:
            prompt+='\nThe previous response was not accepted. Use one allowed message tool with a concrete answer. '+str(exc)[:600]
    raise ValueError('Message Manager did not return a valid disposition after bounded correction')


def reply(store,run,message,content):
    store.execute('''INSERT INTO session_messages(id,session_id,run_id,role,content,created_at)
        VALUES (?,?,?,'assistant',?,?) ON CONFLICT(id) DO NOTHING''',
        ('message:'+message['id'],run['session_id'],run['id'],content,store.now()))
    from .conversation_memory import message_recorded
    message_recorded(store,run)


def fail(store,run,task,code):
    rows=store.query('SELECT * FROM run_inputs WHERE id=? AND run_id=?',(task['spec']['message_input_id'],run['id']))
    if not rows:return
    message=rows[0]
    if message['status']!='ROUTING':return
    store.execute("UPDATE run_inputs SET status='FAILED',error_code=?,applied_at=? WHERE id=?",(code,store.now(),message['id']))
    store.execute("UPDATE control_jobs SET status='failed',error_code=?,updated_at=? WHERE run_id=? AND id=?",(code,store.now(),run['id'],task['id']))
    reply(store,run,message,'这条消息暂未处理成功（'+code+'）。本条消息没有修改或暂停原任务，请重试发送。')
    graph.event(store,run,'message_failed',{'input_id':message['id'],'code':code})


def apply(store,run,task,data):
    decision=MessageDecision.model_validate(data.get('message_decision'))
    frozen=json.loads(task['snapshot_json'])
    if data.get('snapshot_digest')!=task['snapshot_digest'] or data.get('input_id')!=frozen['input_id']:
        raise ValueError('Message result does not match its frozen input')
    rows=store.query('SELECT * FROM run_inputs WHERE id=? AND run_id=?',(frozen['input_id'],run['id']))
    if not rows or rows[0]['status']!='ROUTING':raise ValueError('Message was already handled or superseded')
    message=rows[0];answer=decision.answer
    if decision.action=='reply':
        store.execute("UPDATE run_inputs SET status='REPLIED',applied_at=? WHERE id=?",(store.now(),message['id']))
    elif decision.action=='retry':
        current=graph.get(store,run['id']);options={item['id']:item for item in retry_options(store,current)}
        option=options.get(decision.retry_option_id)
        if not option:raise ValueError('Retry option is stale or no longer safe')
        request_id=graph.digest({'message_id':message['id'],'retry_option_id':option['id']})[:32]
        graph.command(store,run['id'],'manager_agent','resume',request_id=request_id)
        store.execute("UPDATE run_inputs SET status='REPLIED',applied_at=? WHERE id=?",(store.now(),message['id']))
        answer+='\n已登记“'+option['label']+'”，系统会再次校验当前状态后执行；不会改变已确认的任务契约。'
    elif decision.action=='new_task' or run['delivered_at'] is not None:
        # Publication may win while the read-only Manager is deciding. Never
        # mutate an already delivered graph; preserve the request in a new Run.
        owned={a['id'] for a in store.assets(run['project_id'])}
        auth={**run['authorization'],'goal':message['content'],'require_plan_confirmation':True,
              'content_fingerprint':graph.content_fingerprint(store,run['project_id']),
              'allowed_assets':[a for a in run['authorization']['allowed_assets'] if a in owned],
              'allowed_asset_digests':{k:v for k,v in run['authorization']['allowed_asset_digests'].items() if k in owned}}
        successor=graph.create(store,run['project_id'],None,auth,parent_run_id=run['id'],scope_confirmed=False,record_message=False)
        store.execute("UPDATE run_inputs SET status='SUPERSEDED',successor_run_id=?,applied_at=? WHERE id=?",(successor['id'],store.now(),message['id']))
        answer+='\n已创建独立任务，原任务和已交付成果保持不变；新任务会先给出方案供你确认。'
    else:
        from .run_inputs import activate
        activate(store,run,message)
        answer+='\n已接收修改，等待当前已开始的工作安全结束后重新规划；尚未执行此修改。'
    reply(store,run,message,answer)
    store.execute("UPDATE control_jobs SET status='succeeded',updated_at=? WHERE run_id=? AND id=?",(store.now(),run['id'],task['id']))
    graph.event(store,run,'message_answered',{'input_id':message['id'],'action':decision.action,'snapshot_at':frozen['snapshot_at']})


def business_inflight(store,identifier):
    message_attempts="SELECT a.id FROM execution_attempts a JOIN control_jobs c ON c.run_id=a.run_id AND c.id=a.step_id WHERE a.run_id=? AND c.control_kind='MESSAGE'"
    leases=store.query("SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>? AND attempt NOT IN ("+message_attempts+")",(identifier,store.now(),identifier))
    cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete' AND attempt NOT IN ("+message_attempts+")",(identifier,identifier))
    return leases,cleanup


def recover(store,run):
    for task in run['control_jobs']:
        if task['control_kind']!='MESSAGE' or task['status'] in ('succeeded','failed','cancelled'):continue
        if run['status'] in ('CANCELLING','FAILED','CANCELLED'):
            fail(store,run,task,'task_stopped_before_message_reply');continue
        if not task['lease_id']:
            if store.now()-task['created_at']>180:fail(store,run,task,'message_dispatch_timeout')
            continue
        lease=store.query('SELECT active,expires FROM execution_leases WHERE attempt=?',(task['lease_id'],))
        if lease and lease[0]['active'] and lease[0]['expires']>store.now():
            if task['status']=='ready':store.execute("UPDATE control_jobs SET status='running' WHERE run_id=? AND id=?",(run['id'],task['id']))
            continue
        if store.query("SELECT id FROM task_results WHERE run_id=? AND task_id=? AND lease_generation=? AND status='staged'",(run['id'],task['id'],task['lease_generation'])):continue
        if store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(task['lease_id'],)):continue
        if task['recovery_count']>=3:fail(store,run,task,'message_recovery_exhausted');continue
        store.execute("UPDATE control_jobs SET status='ready',lease_id='',recovery_count=recovery_count+1,infrastructure_interruptions=infrastructure_interruptions+1,next_run_at=?,updated_at=? WHERE run_id=? AND id=?",
                      (store.now()+1,store.now(),run['id'],task['id']))
