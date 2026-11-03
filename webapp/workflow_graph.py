"""Versioned append-only workflows. Durable commands and proposals carry no authority.

Only the orchestrator applies business transitions. Execution leases, immutable
artifacts and operation contracts remain owned by their existing subsystems.
"""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .quality_policy import Acceptance
from .operations import InputRef

TERMINAL={'succeeded','failed','cancelled'}


class AuthorizationRequired(ValueError):pass


def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class Authorization(Contract):
    goal:str=Field(min_length=1,max_length=8000)
    allowed_assets:list[str]=Field(max_length=100)
    allowed_operations:list[str]=Field(max_length=100)
    allowed_agent_profiles:list[str]=Field(default_factory=list,max_length=20)
    operation_implementations:dict[str,str]=Field(default_factory=dict,max_length=100)
    agent_profile_digests:dict[str,str]=Field(default_factory=dict,max_length=20)
    provider_call_budget:dict[str,int]=Field(default_factory=dict,max_length=50)
    cost_budget:float|None=Field(default=None,ge=0)
    model_token_budget:int=Field(default=80000,ge=1024,le=2000000)
    wall_time_budget:int=Field(default=7200,ge=60,le=86400)
    max_tasks:int=Field(default=64,ge=1,le=256)
    max_planning_rounds:int=Field(default=16,ge=1,le=64)
    delivery_targets:list[str]=Field(min_length=1,max_length=20)
    content_fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    semantic_policy_version:Literal['media-semantics-v1']='media-semantics-v1'

    @model_validator(mode='after')
    def valid(self):
        from .operations import get
        from .specialist_profiles import get as profile
        for op in self.allowed_operations:
            if op!='script':get(op)
        for name in self.allowed_agent_profiles:profile(name)
        for name,limit in self.provider_call_budget.items():
            if name not in self.allowed_operations or type(limit)!=int or not 0<=limit<=100:
                raise ValueError('Invalid authorized provider budget')
        if any(not item.strip() for item in self.delivery_targets):raise ValueError('Empty delivery target')
        return self


class DecisionAnswer(Contract):
    decision_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    snapshot_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    action:Literal['approve','reject']
    authorization:Authorization|None=None
    feedback:str=Field(default='',max_length=4000)


class SubjectSemantics(Contract):
    mode:Literal['subject_cutout','foreground_cutout']
    include_relations:list[Literal['body','clothing','worn','held','carried']]=Field(default_factory=list,max_length=5)
    exclude_relations:list[Literal['held','carried']]=Field(default_factory=list,max_length=2)
    @model_validator(mode='after')
    def defaults(self):
        if not self.include_relations:
            self.include_relations=(['body','clothing','worn'] if self.mode=='subject_cutout'
                                    else ['body','clothing','worn','held','carried'])
        if not self.exclude_relations and self.mode=='subject_cutout':self.exclude_relations=['held','carried']
        if set(self.include_relations)&set(self.exclude_relations):raise ValueError('Subject relations cannot be both included and excluded')
        return self


class TemporalSemantics(Contract):
    action:str=Field(min_length=1,max_length=200)
    start_by_fraction:float=Field(default=.1,ge=0,le=1)
    start_by_seconds:float=Field(default=1,ge=0,le=60)
    required_coverage:float=Field(default=.85,ge=0,le=1)
    max_interruption_seconds:float=Field(default=.5,ge=0,le=30)
    transition:Literal['immediate','continuous','gradual']='continuous'
    min_transition_fraction:float=Field(default=.5,ge=0,le=1)
    allow_abrupt_jump:bool=False


class MaskRepairSemantics(Contract):
    level:Literal['A','B','C','D']
    strategy:Literal['local_edge_repair','local_hole_repair','resegment','clarify_scope']
    max_changed_fraction:float|None=Field(default=None,ge=0,le=1)
    @model_validator(mode='after')
    def strategy_matches_level(self):
        expected={'A':'local_edge_repair','B':'local_hole_repair','C':'resegment','D':'clarify_scope'}[self.level]
        if self.strategy!=expected:raise ValueError('Mask repair level and strategy conflict')
        if self.level in ('A','B') and self.max_changed_fraction is None:raise ValueError('Local mask repair requires a maximum changed fraction')
        return self


class TaskSemantics(Contract):
    policy_version:Literal['media-semantics-v1']='media-semantics-v1'
    subject:SubjectSemantics|None=None
    consistency_mode:Literal['strict_reference','character_consistent','creative']='character_consistent'
    mask_repair:MaskRepairSemantics|None=None
    temporal:list[TemporalSemantics]=Field(default_factory=list,max_length=20)


class Task(Contract):
    id:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    type:Literal['operation','script','agent']
    name:str=Field(min_length=1,max_length=120)
    objective:str=Field(min_length=1,max_length=2000)
    profile:str|None=None
    operation:str|None=None
    implementation:str|None=None
    inputs:dict[str,InputRef]=Field(default_factory=dict,max_length=100)
    parameters:dict=Field(default_factory=dict)
    output_roles:dict[str,str]=Field(default_factory=dict,max_length=20)
    acceptance:list[Acceptance]=Field(min_length=1,max_length=20)
    argv:list[str]=Field(default_factory=list,max_length=100,
        description='script only: complete process argv including executable, e.g. ["python", "-c", "Python source"]. Not a list of flags for an implicit tool. Omit for operation/agent.')
    outputs:list[str]=Field(default_factory=list,max_length=20)
    preflight:list[str]=Field(default_factory=list,max_length=20)
    config_files:list[str]=Field(default_factory=list,max_length=20)
    timeout_seconds:int=Field(default=900,ge=10,le=3600)
    retry_policy:Literal['never','safe_once']='never'
    on_failure:Literal['pause','diagnose']='pause'
    basic_reason:str=Field(default='',max_length=1000)
    semantics:TaskSemantics=Field(default_factory=TaskSemantics)
    delivery_stage:bool=False

    @model_validator(mode='after')
    def valid(self):
        if self.type=='agent':
            from .specialist_profiles import get
            profile=get(self.profile)
            if profile.id in ('media_planner','delivery_reviewer'):raise ValueError('Control profiles cannot be delegated as business tasks')
            if self.operation or self.implementation or self.argv or self.parameters or self.preflight or self.config_files:
                raise ValueError('Agent profiles do not accept executable policy overrides')
            if not self.output_roles:raise ValueError('Agent task must declare output roles')
            if self.timeout_seconds>profile.timeout_seconds:raise ValueError('Agent timeout exceeds profile')
            for role,name in self.output_roles.items():
                if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',role) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}',name):
                    raise ValueError('Invalid Agent output role or filename')
            if self.outputs and set(self.outputs)!=set(self.output_roles.values()):raise ValueError('Agent outputs must match its declared output role filenames')
            from .specialist_profiles import result_model
            schema=result_model(profile)
            if schema:
                if any(not name.endswith('.json') for name in self.output_roles.values()):raise ValueError('This Agent profile produces JSON reports only')
                fields=set(schema.model_fields)
                if any(check.kind=='json' and not set(check.required_keys)<=fields for check in self.acceptance):
                    raise ValueError('Acceptance requires fields outside the fixed Agent result schema')
        else:
            if self.profile:raise ValueError('Only agent tasks accept a profile')
            if self.type=='script' and self.argv and self.argv[0].startswith('-'):
                raise ValueError('script argv must start with an executable such as python, not an option; no implicit implementation exists')
            from .workflows import Step
            step=Step.model_validate(self.execution_spec())
            self.parameters=step.parameters;self.implementation=step.implementation
        if not self.basic_reason and all(c.kind=='file' for c in self.acceptance):
            raise ValueError('Basic-only acceptance requires a reason')
        if any(c.stage=='final' for c in self.acceptance) and not self.delivery_stage:
            raise ValueError('Final-stage acceptance belongs only on an explicit delivery-stage task')
        return self

    def execution_spec(self,dependencies=()):
        return {'id':self.id,'kind':self.type,'name':self.name,'goal':self.objective,
                'operation':self.operation,'implementation':self.implementation,
                'inputs':{k:v.model_dump() for k,v in self.inputs.items()},'parameters':self.parameters,
                'output_roles':self.output_roles,'outputs':self.outputs or (list(self.output_roles.values()) if self.type=='agent' else []),
                'argv':self.argv,'preflight':self.preflight,'config_files':self.config_files,
                'timeout':self.timeout_seconds,'retry_safe':self.retry_policy=='safe_once',
                'acceptance':[a.model_dump() for a in self.acceptance],'repairs':[],
                'on_failure':self.on_failure,'basic_reason':self.basic_reason,'depends_on':list(dependencies)}


class Dependency(Contract):
    upstream:str=Field(min_length=1,max_length=40)
    downstream:str=Field(min_length=1,max_length=40)


class Proposal(Contract):
    proposal_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    base_graph_version:int=Field(ge=0)
    reason:str=Field(min_length=1,max_length=2000)
    add_tasks:list[Task]=Field(default_factory=list,max_length=64)
    revise_tasks:list[Task]=Field(default_factory=list,max_length=16,
        description='New immutable revisions of existing failed logical tasks. IDs must already exist; unchanged fields remain frozen by the server.')
    add_dependencies:list[Dependency]=Field(default_factory=list,max_length=512)
    replan_after:list[str]=Field(default_factory=list,max_length=64)
    sealed:bool=False


def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS workflow_runs(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL,turn_id TEXT NOT NULL UNIQUE,
          goal TEXT NOT NULL,status TEXT NOT NULL,graph_version INTEGER NOT NULL DEFAULT 0,
          sealed INTEGER NOT NULL DEFAULT 0,authorization_json TEXT NOT NULL,
          authorization_version INTEGER NOT NULL DEFAULT 1,replan_after_json TEXT NOT NULL DEFAULT '[]',
          repair_context_json TEXT NOT NULL DEFAULT '{}',
          next_reconcile_at REAL NOT NULL,reconcile_owner TEXT NOT NULL DEFAULT '',
          reconcile_until REAL NOT NULL DEFAULT 0,reconcile_generation INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,terminal_reason TEXT NOT NULL DEFAULT '',
          delivery_json TEXT NOT NULL DEFAULT '[]',delivered_at REAL);
        CREATE TABLE IF NOT EXISTS workflow_asset_reuse(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,
          revision INTEGER NOT NULL,path TEXT NOT NULL,artifact_id TEXT NOT NULL,sha256 TEXT NOT NULL,
          asset_id TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS workflow_due ON workflow_runs(next_reconcile_at,status);
        CREATE TABLE IF NOT EXISTS workflow_tasks(
          workflow_id TEXT NOT NULL,id TEXT NOT NULL,type TEXT NOT NULL,spec_json TEXT NOT NULL,
          profile_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'execute',internal INTEGER NOT NULL DEFAULT 0,
          created_graph_version INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
          repair_count INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,next_run_at REAL NOT NULL DEFAULT 0,
          wait_reason TEXT NOT NULL DEFAULT '',candidate_result_json TEXT NOT NULL DEFAULT '[]',
          accepted_result_json TEXT NOT NULL DEFAULT '[]',quality_json TEXT NOT NULL DEFAULT '{}',
          bindings_json TEXT NOT NULL DEFAULT '{}',external_id TEXT NOT NULL DEFAULT '',
          error_code TEXT NOT NULL DEFAULT '',error_detail TEXT NOT NULL DEFAULT '',
          lease_id TEXT NOT NULL DEFAULT '',lease_generation INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(workflow_id,id));
        CREATE TABLE IF NOT EXISTS workflow_task_revisions(
          workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,spec_json TEXT NOT NULL,
          created_graph_version INTEGER NOT NULL,repair_root INTEGER NOT NULL DEFAULT 0,
          supersedes_revision INTEGER,reason TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,
          PRIMARY KEY(workflow_id,task_id,revision));
        CREATE TABLE IF NOT EXISTS workflow_plan_tasks(
          workflow_id TEXT NOT NULL,graph_version INTEGER NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,
          selected_at REAL NOT NULL,PRIMARY KEY(workflow_id,graph_version,task_id));
        CREATE INDEX IF NOT EXISTS workflow_ready ON workflow_tasks(status,next_run_at);
        CREATE TABLE IF NOT EXISTS workflow_dependencies(
          workflow_id TEXT NOT NULL,upstream_task_id TEXT NOT NULL,downstream_task_id TEXT NOT NULL,
          created_graph_version INTEGER NOT NULL,PRIMARY KEY(workflow_id,upstream_task_id,downstream_task_id));
        CREATE TABLE IF NOT EXISTS planner_runs(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,base_graph_version INTEGER NOT NULL,
          authorization_version INTEGER NOT NULL,trigger_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,
          snapshot_digest TEXT NOT NULL,status TEXT NOT NULL,proposal_json TEXT NOT NULL DEFAULT '{}',
          lease_id TEXT NOT NULL DEFAULT '',lease_generation INTEGER NOT NULL DEFAULT 0,
          proposal_id TEXT,applied_graph_version INTEGER,attempts INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,error_code TEXT NOT NULL DEFAULT '',
          UNIQUE(workflow_id,task_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_planner ON planner_runs(workflow_id)
          WHERE status IN ('pending','running','staged');
        CREATE UNIQUE INDEX IF NOT EXISTS unique_graph_proposal ON planner_runs(workflow_id,proposal_id)
          WHERE proposal_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS workflow_commands(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,
          payload_json TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,applied_at REAL,
          error_code TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS task_results(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,
          lease_id TEXT NOT NULL,lease_generation INTEGER NOT NULL,outcome TEXT NOT NULL,result_json TEXT NOT NULL,
          artifact_manifest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'staged',created_at REAL NOT NULL,committed_at REAL,
          UNIQUE(workflow_id,task_id,task_revision,lease_generation));
        CREATE TABLE IF NOT EXISTS workflow_decisions(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,kind TEXT NOT NULL,graph_version INTEGER NOT NULL,
          authorization_version INTEGER NOT NULL,snapshot_digest TEXT NOT NULL,request_json TEXT NOT NULL,
          answer_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_reviews(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,purpose TEXT NOT NULL,
          subject_task TEXT NOT NULL DEFAULT '',graph_version INTEGER NOT NULL,snapshot_json TEXT NOT NULL,
          snapshot_digest TEXT NOT NULL,status TEXT NOT NULL,decision_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_graph_review ON workflow_reviews(workflow_id,purpose,subject_task)
          WHERE status IN ('pending','running','staged');
        CREATE TABLE IF NOT EXISTS workflow_graph_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,workflow_id TEXT NOT NULL,kind TEXT NOT NULL,
          graph_version INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_model_reservations(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
          reserved_tokens INTEGER NOT NULL,used_tokens INTEGER,state TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_orchestrators(
          id TEXT PRIMARY KEY,release TEXT NOT NULL,heartbeat REAL NOT NULL,draining INTEGER NOT NULL DEFAULT 0,
          reconciles INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,last_duration REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS workflow_reconcile_failures(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,reconcile_generation INTEGER NOT NULL,
          exception_type TEXT NOT NULL,error_code TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_inspections(
          id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
          reference TEXT NOT NULL,snapshot_digest TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_agent_outputs(
          lease_id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,task_id TEXT NOT NULL,
          generation INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL);
        ''')
    store._dynamic_schema=True


def get(store,identifier):
    if not hasattr(store,'_dynamic_schema'):
        store._dynamic_schema=bool(store.query("SELECT name FROM sqlite_master WHERE name='workflow_runs'"))
    if not store._dynamic_schema:return None
    rows=store.query('SELECT * FROM workflow_runs WHERE id=? OR turn_id=?',(identifier,identifier))
    if not rows:return None
    run=rows[0]
    for field in ('authorization','replan_after','repair_context','delivery'):run[field]=json.loads(run.pop(field+'_json'))
    run['sealed']=bool(run['sealed'])
    run['tasks']=store.query('SELECT * FROM workflow_tasks WHERE workflow_id=? ORDER BY created_graph_version,created_at,id',(run['id'],))
    for task in run['tasks']:
        for field in ('spec','profile','candidate_result','accepted_result','quality','bindings'):
            task[field]=json.loads(task.pop(field+'_json'))
    run['dependencies']=store.query('SELECT upstream_task_id,downstream_task_id FROM workflow_dependencies WHERE workflow_id=?',(run['id'],))
    return run


def event(store,run,kind,payload=None):
    store.execute('INSERT INTO workflow_graph_events(workflow_id,kind,graph_version,payload_json,created_at) VALUES (?,?,?,?,?)',
                  (run['id'],kind,run['graph_version'],encode(payload or {}),store.now()))
    from .agent_store import event as agent_event
    from .telemetry import emit
    agent_event(store,run['turn_id'],'workflow_graph',{'workflow_id':run['id'],'graph_version':run['graph_version'],'kind':kind,**(payload or {})})
    fields={'task_id':run['turn_id'],'workflow_id':run['id'],'graph_version':run['graph_version'],
            'sealed':run['sealed'],'project_id':run['project_id']}
    for key in ('proposal_id','error_code','revision','outcome'):
        if key in (payload or {}) and payload[key]!='':fields[key]=payload[key]
    if payload and payload.get('status'):fields['new_state']=payload['status']
    if payload and payload.get('reason') and re.fullmatch(r'[a-z_]{1,80}',str(payload['reason'])):fields['reason']=payload['reason']
    if payload and payload.get('task_id'):fields['step_id']=payload['task_id']
    store.notify_after_commit(lambda:emit('workflow.'+kind,**fields))


def content_fingerprint(store,project):
    from .plan_readiness import fingerprint
    return fingerprint(store,project)


def create(store,project,turn,raw):
    authorization=bind_authorization(raw)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT project,mode FROM agent_turns WHERE id=?',(turn,)).fetchone()
        if not row or row['project']!=project or row['mode']!='execute':raise ValueError('Workflow requires an owned execute turn')
        existing=db.execute('SELECT id,authorization_json FROM workflow_runs WHERE turn_id=?',(turn,)).fetchone()
        if existing:
            if existing['authorization_json']!=encode(authorization.model_dump()):raise ValueError('Workflow authorization is immutable')
            return get(store,existing['id'])
        owned={r['id'] for r in db.execute('SELECT id FROM assets WHERE project=?',(project,))}
        if not set(authorization.allowed_assets)<=owned:raise ValueError('Asset outside project')
        if authorization.content_fingerprint!=content_fingerprint(store,project):raise ValueError('Approved content has changed')
        identifier=uuid.uuid4().hex;now=store.now()
        db.execute('''INSERT INTO workflow_runs(id,project_id,turn_id,goal,status,authorization_json,next_reconcile_at,created_at,updated_at)
            VALUES (?,?,?,?,'planning',?,?,?,?)''',(identifier,project,turn,authorization.goal,encode(authorization.model_dump()),now,now,now))
        from .task_lifecycle import ensure
        ensure(db,turn,now)
        db.execute('UPDATE execution_limits SET deadline_at=?,budget_seconds=? WHERE task_id=?',
                   (now+authorization.wall_time_budget,authorization.wall_time_budget,turn))
        db.execute("UPDATE agent_turns SET status='waiting',updated=? WHERE id=?",(now,turn))
        run=get(store,identifier);event(store,run,'created');return run


def bind_authorization(raw,*,implementations=None):
    authorization=Authorization.model_validate(raw)
    from .operations import get as operation
    from .operation_lifecycle import for_operation
    from .specialist_profiles import get as profile
    if implementations is None:
        implementations={name:for_operation(operation(name)).id for name in authorization.allowed_operations if name!='script'}
    else:
        implementations={name:for_operation(operation(name,implementations[name])).id
                         for name in authorization.allowed_operations if name!='script'}
    profiles={name:digest(profile(name).snapshot()) for name in authorization.allowed_agent_profiles}
    if any(implementations.get(name)!=value for name,value in authorization.operation_implementations.items()):
        raise ValueError('Requested operation implementation is no longer current')
    if any(profiles.get(name)!=value for name,value in authorization.agent_profile_digests.items()):
        raise ValueError('Requested Agent policy is no longer current')
    authorization.operation_implementations=implementations
    authorization.agent_profile_digests=profiles
    return authorization


def command(store,identifier,actor,action,payload=None,request_id=None):
    if action not in ('cancel','pause','resume','answer'):raise ValueError('Unsupported workflow command')
    payload=payload or {};request_id=request_id or uuid.uuid4().hex
    with store.connect() as db:
        run=get(store,identifier)
        if not run:raise ValueError('Unknown workflow')
        previous=db.execute('SELECT * FROM workflow_commands WHERE id=?',(request_id,)).fetchone()
        if previous:
            if (previous['workflow_id'],previous['actor'],previous['action'],previous['payload_json'])!=(run['id'],actor,action,encode(payload)):
                raise ValueError('Command identity was reused with different content')
            return dict(previous)
        error='workflow_terminal' if run['status'] in TERMINAL else 'cancellation_in_progress' if run['status']=='cancelling' and action!='cancel' else ''
        db.execute('''INSERT INTO workflow_commands(id,workflow_id,actor,action,payload_json,status,created_at,applied_at,error_code)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (request_id,run['id'],actor,action,encode(payload),'rejected' if error else 'pending',store.now(),store.now() if error else None,error))
        db.execute('UPDATE workflow_runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        return {'id':request_id,'status':'rejected' if error else 'pending','error_code':error}


def validate_proposal(store,run,raw,repair_scope=None):
    proposal=Proposal.model_validate(raw)
    if run['sealed'] or run['status'] not in ('planning','running'):raise ValueError('Workflow cannot accept proposals')
    if proposal.base_graph_version!=run['graph_version']:raise ValueError('Stale graph version')
    authorization=Authorization.model_validate(run['authorization'])
    known_assets={asset['id'] for asset in store.assets(run['project_id'])}
    existing={t['id']:t for t in run['tasks'] if not t['internal']}
    added={task.id:task for task in proposal.add_tasks}
    revised={task.id:task for task in proposal.revise_tasks}
    if len(added)!=len(proposal.add_tasks) or set(added)&set(existing):raise ValueError('Task IDs must be unique for the whole workflow')
    if len(revised)!=len(proposal.revise_tasks) or not set(revised)<=set(existing) or set(revised)&set(added):
        raise ValueError('Task revisions must name unique existing logical tasks')
    if len(existing)+len(added)>authorization.max_tasks:raise AuthorizationRequired('Task budget exhausted')
    edges={(r['upstream_task_id'],r['downstream_task_id']) for r in run['dependencies']}
    for edge in proposal.add_dependencies:
        if edge.downstream not in added:raise ValueError('Dependencies of existing tasks are immutable')
        if edge.upstream not in existing and edge.upstream not in added:raise ValueError('Unknown dependency')
        if (edge.upstream,edge.downstream) in edges:raise ValueError('Duplicate dependency')
        edges.add((edge.upstream,edge.downstream))
    remaining=set(existing)|set(added);order=[]
    while remaining:
        ready=sorted(n for n in remaining if not any(child==n and parent in remaining for parent,child in edges))
        if not ready:raise ValueError('Dependency cycle')
        order.extend(ready);remaining.difference_update(ready)
    from .operations import get as operation
    if revised:
        if repair_scope is None:raise ValueError('Task revisions require a frozen repair trigger')
        allowed=set(existing) if repair_scope=='' else set()
        pending=[repair_scope] if repair_scope else []
        while pending:
            current_id=pending.pop()
            if current_id in allowed:continue
            allowed.add(current_id);pending.extend(upstream for upstream,downstream in edges if downstream==current_id)
        if not set(revised)<=allowed:raise ValueError('Repair revisions are limited to the failed task or dependency ancestors')
    specs={key:Task.model_validate(t['spec']) for key,t in existing.items()}
    for identifier,replacement in revised.items():
        current=specs[identifier];row=existing[identifier]
        if row['status'] not in ('paused','succeeded'):
            raise ValueError('Only a failed task or accepted dependency ancestor can be revised')
        if row['lease_id']:
            active=store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',(row['lease_id'],store.now()))
            cleaning=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(row['lease_id'],))
            if active or cleaning:raise ValueError('Task revision must wait for execution and cleanup to finish')
        if row.get('repair_count',0)>=2:raise ValueError('Task repair budget exhausted')
        frozen=('id','type','name','objective','profile','operation','implementation','inputs','output_roles','acceptance','semantics','delivery_stage',
                'outputs','preflight','config_files','timeout_seconds','retry_policy','on_failure','basic_reason')
        if any(getattr(current,name)!=getattr(replacement,name) for name in frozen):
            raise ValueError('A repair revision cannot change objective, authorization, inputs, outputs, acceptance or execution policy')
        if current.type=='script':
            if replacement.parameters!=current.parameters:raise ValueError('Script repair changes argv only')
        elif current.type=='operation':
            if replacement.argv!=current.argv:raise ValueError('Operation repair changes parameters only')
            if not operation(current.operation,current.implementation).retry_safe:
                raise ValueError('An external or non-idempotent operation requires a new authorized task')
        else:raise ValueError('Agent policy revisions require a new task')
        specs[identifier]=replacement
    specs.update(added)
    calls={}
    for task in specs.values():
        if task.on_failure=='diagnose' and 'media_failure_diagnostician' not in authorization.allowed_agent_profiles:
            raise AuthorizationRequired('Failure diagnosis requires an authorized diagnostic Agent')
        if task.type!='agent':
            from .workflows import Step
            Step.model_validate(task.execution_spec([parent for parent,child in edges if child==task.id]))
        if task.type=='agent':
            if task.profile not in authorization.allowed_agent_profiles:raise AuthorizationRequired('Agent profile requires additional authorization')
            from .specialist_profiles import get as profile
            if authorization.agent_profile_digests.get(task.profile)!=digest(profile(task.profile).snapshot()):
                raise AuthorizationRequired('Agent policy changed and requires renewed authorization')
        else:
            name=task.operation if task.type=='operation' else 'script'
            if name not in authorization.allowed_operations:raise AuthorizationRequired('Operation requires additional authorization')
            if task.type=='operation' and authorization.operation_implementations.get(name)!=task.implementation:
                raise AuthorizationRequired('Operation implementation requires renewed authorization')
            if task.type=='operation' and operation(task.operation,task.implementation).provider:
                calls[name]=calls.get(name,0)+1
                if authorization.cost_budget is not None:
                    raise AuthorizationRequired('A finite cost budget requires a verified provider price bound; unavailable')
        for ref in task.inputs.values():
            if ref.asset:
                if ref.asset not in known_assets:
                    raise ValueError('Input asset does not exist in this project')
                if ref.asset not in authorization.allowed_assets:raise AuthorizationRequired('Input asset is not authorized')
            else:
                if (ref.step,task.id) not in edges:raise ValueError('Input must declare its upstream dependency')
                parent=specs[ref.step]
                roles=operation(parent.operation,parent.implementation).outputs if parent.type=='operation' else parent.output_roles
                if ref.role not in roles:raise ValueError('Unknown upstream output role')
    if any(count>authorization.provider_call_budget.get(name,0) for name,count in calls.items()):
        raise AuthorizationRequired('Cumulative provider call budget exceeded')
    if not set(proposal.replan_after)<=set(specs):raise ValueError('Unknown replan trigger')
    if proposal.sealed and proposal.replan_after:raise ValueError('Sealed graph cannot request another planning round')
    unresolved={identifier for identifier,task in existing.items() if task['status']=='paused'}-set(revised)
    if proposal.sealed and unresolved:
        raise ValueError('Sealed repair plan must revise every paused required task: '+','.join(sorted(unresolved)))
    if not proposal.sealed and not proposal.replan_after:raise ValueError('Open graph needs an explicit replan trigger')
    if not proposal.sealed and not any(t in added or existing[t]['status'] not in TERMINAL for t in proposal.replan_after):
        raise ValueError('Replan trigger is already exhausted')
    if not specs:raise ValueError('A workflow must contain at least one business task')
    return proposal,[added[name] for name in order if name in added],revised,edges


def execution_view(run):
    """Stable execution adapter shape; it confers no mutation authority."""
    steps=[]
    for task in run['tasks']:
        if task['internal']:continue
        dependencies=[d['upstream_task_id'] for d in run['dependencies'] if d['downstream_task_id']==task['id']]
        spec=Task.model_validate(task['spec']).execution_spec(dependencies)
        phase_status={'execute':'pending','poll':'waiting','collect':'waiting','validate':'verifying'}.get(task['phase'],'pending')
        steps.append({'id':task['id'],'workflow':run['id'],'spec':spec,
            'status':'succeeded' if task['status']=='succeeded' else phase_status,
            'revision':task['revision'],'attempts':task['attempts'],'failures':task['attempts'],
            'external_id':task['external_id'],'bindings':task['bindings'],'candidate':task['candidate_result'],
            'result':task['accepted_result'],'quality':task['quality'],'next_poll':task['next_run_at'],
            'started':task['created_at'],'updated':task['updated_at'],'error':task['error_detail'],
            'error_code':task['error_code'],'wait_reason':task['wait_reason'],'repair_count':task.get('repair_count',0),
            'diagnostics':[],'task_status':task['status']})
    return {'id':run['id'],'project':run['project_id'],'turn_id':run['turn_id'],'name':run['goal'],
            'status':run['status'],'steps':steps,'dynamic':True,'graph_version':run['graph_version'],'sealed':run['sealed'],
            'agent_review':None,'user_requests':[],'error':run['terminal_reason'],'plan_id':'',
            'created':run['created_at'],'updated':run['updated_at'],
            'definition':{'key':run['id'],'name':run['goal'],'steps':[s['spec'] for s in steps]}}
