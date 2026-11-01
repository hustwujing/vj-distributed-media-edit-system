"""Typed user decisions: quality approval is never external-call authorization."""
import json
import uuid
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Literal

class UserRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['quality','clarification','selection','external_call','plan_change']='quality'
    question:str=Field(min_length=1,max_length=2000)
    options:list[str]=Field(default_factory=list,max_length=10)
    output_role: str | None = Field(default=None,pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    candidates: dict[str,str] = Field(default_factory=dict,max_length=10)
    @model_validator(mode='after')
    def meaningful(self):
        if not self.question.strip() or any(not choice.strip() for choice in self.options):raise ValueError('问题与选项不得为空')
        if len(set(self.options))!=len(self.options):raise ValueError('选项必须唯一')
        return self

class Answer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['approve','reject','answer']
    value:str=Field(default='',max_length=2000)
    revision:int=Field(ge=0)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS workflow_user_requests(
       id TEXT PRIMARY KEY,workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
       kind TEXT NOT NULL,question TEXT NOT NULL,options TEXT NOT NULL,proposal TEXT NOT NULL,
       status TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL
    );''')


def create(store,w,s,request,proposal=None):
    request=UserRequest.model_validate(request)
    if request.output_role:
        if request.kind!='selection' or not request.candidates or set(request.options)!=set(request.candidates):raise ValueError('产物选择需要明确选项与文件映射')
        if not set(request.candidates.values()).issubset(s['candidate']):raise ValueError('候选文件不属于当前步骤')
        from .operations import for_step
        operation=for_step(s['spec'])
        declared=operation.outputs if operation else s['spec'].get('output_roles',{})
        if request.output_role not in declared:raise ValueError('未登记的产物角色')
        proposal={'output_role':request.output_role,'candidates':request.candidates}
    identifier=uuid.uuid4().hex
    store.execute('INSERT INTO workflow_user_requests(id,workflow,step,revision,kind,question,options,proposal,status,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (identifier,w['id'],s['id'],s['revision'],request.kind,request.question,json.dumps(request.options),json.dumps(proposal),'pending',store.now(),store.now()))
    return identifier


def pending(store,wid):
    requests=[]
    for row in store.query("SELECT id,step,revision,kind,question,options,proposal FROM workflow_user_requests WHERE workflow=? AND status='pending' ORDER BY created",(wid,)):
        proposal=json.loads(row.pop('proposal'))
        row['options']=json.loads(row['options'])
        if row['kind']=='external_call' and proposal:
            current=store.query('SELECT spec FROM agent_steps WHERE workflow=? AND id=?',(wid,row['step']))
            original=json.loads(current[0]['spec']) if current else {}
            row['changes']={k:{'before':original.get(k),'after':v} for k,v in proposal.items() if original.get(k)!=v and k in ('parameters','argv')}
        requests.append(row)
    return requests


def answer(store,turn_id,request_id,raw,actor):
    from . import workflows,workflow_review
    from .task_lifecycle import check
    value=Answer.model_validate(raw)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');check(db,turn_id)
        w=workflows.get(store,turn_id)
        row=db.execute('SELECT * FROM workflow_user_requests WHERE id=? AND workflow=?',(request_id,w['id'] if w else '')).fetchone()
        if not row or row['status']!='pending' or w['status']!='paused':raise ValueError('用户决策已处理、失效或任务尚未暂停')
        s=next(x for x in w['steps'] if x['id']==row['step'])
        if value.revision!=s['revision'] or s['revision']!=row['revision']:raise ValueError('决策版本已变化')
        lease=db.execute('SELECT active,expires FROM execution_leases WHERE turn_id=? AND active=1 AND expires>? LIMIT 1',(turn_id,store.now())).fetchone()
        if lease and lease['active'] and lease['expires']>store.now():raise ValueError('请等待当前执行资源回收')
        if row['kind']=='quality':
            if value.action not in ('approve','reject'):raise ValueError('质量请求需要明确确认或拒绝')
            workflows.quality_action(store,turn_id,s['id'],s['revision'],value.action,actor,value.value)
        elif row['kind']=='external_call':
            if value.action not in ('approve','reject'):raise ValueError('外部调用需明确批准或拒绝')
            if value.action=='approve':
                proposal=json.loads(row['proposal'])
                if not proposal:raise ValueError('没有明确外部重生成提案')
                workflows.quality_action(store,turn_id,s['id'],s['revision'],'repair',actor,value.value,replacement=proposal,confirm_external=True)
        elif row['kind']=='plan_change':
            if value.action=='approve':
                workflows.cancel(store,turn_id)
                # The UI restores feedback to the composer only after cancellation.
            elif value.action!='reject':raise ValueError('计划变更需要明确确认或拒绝')
        else:
            if value.action!='answer' or not value.value.strip():raise ValueError('请回答问题')
            choices=json.loads(row['options'])
            if choices and value.value not in choices:raise ValueError('请选择列出的候选')
            workflows.quality_event(store,w,s,actor,'clarification',value.value)
            proposal=json.loads(row['proposal'])
            if proposal and proposal.get('output_role'):
                path=proposal['candidates'][value.value]
                if path not in s['candidate']:raise ValueError('候选已失效')
                bindings={**s['bindings'],'_selected_roles':{**s['bindings'].get('_selected_roles',{}),proposal['output_role']:path}}
                workflows.change(store,w['id'],s['id'],bindings=bindings,status='verifying',error='',error_code='',next_poll=0)
                workflows.state(store,turn_id,'ready',text='已保存候选选择，重新验收后继续。')
            else:workflow_review.queue(store,w,s,'diagnose')
        db.execute("UPDATE workflow_user_requests SET status='answered',answer=?,updated=? WHERE id=?",(value.model_dump_json(),store.now(),request_id))
    return workflows.public(workflows.get(store,turn_id))
