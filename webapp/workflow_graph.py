"""Versioned append-only workflows. Durable commands and proposals carry no authority.

Only the orchestrator applies business transitions. Execution leases, immutable
artifacts and operation contracts remain owned by their existing subsystems.
"""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .quality_policy import Acceptance
from .operations import InputRef

TERMINAL={'succeeded','failed','cancelled'}
RUN_TERMINAL={'SUCCEEDED','FAILED','CANCELLED'}
CANDIDATE_REVIEW_LIMIT=2


class AuthorizationRequired(ValueError):pass


def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


@contextmanager
def atomic_effect(store):
    """A rejected command must roll back even inside a reconcile transaction."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        name='effect_'+uuid.uuid4().hex
        notices=getattr(store._connections,'notices',[]);count=len(notices)
        db.execute('SAVEPOINT '+name)
        try:
            yield db
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT '+name)
            db.execute('RELEASE SAVEPOINT '+name)
            del notices[count:]
            raise
        else:db.execute('RELEASE SAVEPOINT '+name)


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class UserCheckpoint(Contract):
    enabled:bool=False
    stage:Literal['before_publish']='before_publish'
    reason:str=Field(default='',max_length=500)

    @model_validator(mode='after')
    def valid(self):
        if self.enabled and not self.reason.strip():raise ValueError('Enabled user checkpoint requires a user-facing reason')
        if not self.enabled and self.reason:raise ValueError('Disabled user checkpoint cannot carry a reason')
        return self


class ReviewPolicy(Contract):
    technical_review:Literal['required']='required'
    user_checkpoint:UserCheckpoint=Field(default_factory=UserCheckpoint)


DELIVERY_FORMATS=frozenset({'png','jpeg','webp','tif','tiff','mp4','mov','mkv','webm','wav','mp3','m4a','flac',
                            'srt','vtt','ass','json','txt','md','csv','pdf','zip','psd','ora','svg','py',
                            'html','docx','xlsx','pptx','gif','drawio'})


class DeliveryContract(Contract):
    filenames:list[str]=Field(default_factory=list,max_length=20,
        description='Exact final output basenames explicitly understood from the user request.')
    formats:list[str]=Field(default_factory=list,max_length=20,
        description='Required final formats, without a leading dot. This is semantic model output, not host text extraction.')
    roles:list[Literal['report']]=Field(default_factory=list,max_length=4,
        description='Auxiliary output roles explicitly requested for final delivery.')
    excluded_filenames:list[str]=Field(default_factory=list,max_length=20,
        description='Named outputs that may exist internally but must not be delivered.')

    @model_validator(mode='after')
    def valid(self):
        from pathlib import PurePosixPath
        def filename(value):
            path=PurePosixPath(value)
            return (bool(value) and len(value)<=100 and path.name==value and '/' not in value and '\\' not in value
                    and path.suffix.lstrip('.').lower() in DELIVERY_FORMATS|{'jpg'})
        if any(not filename(value) for value in [*self.filenames,*self.excluded_filenames]):
            raise ValueError('Delivery filenames must be supported plain basenames')
        self.formats=list(dict.fromkeys('jpeg' if value.lower()=='jpg' else value.lower() for value in self.formats))
        if any(value not in DELIVERY_FORMATS for value in self.formats):raise ValueError('Unsupported delivery format')
        self.filenames=list(dict.fromkeys(self.filenames));self.excluded_filenames=list(dict.fromkeys(self.excluded_filenames))
        self.roles=list(dict.fromkeys(self.roles))
        if set(self.filenames)&set(self.excluded_filenames):raise ValueError('A delivery filename cannot also be excluded')
        for value in self.filenames:
            extension='jpeg' if value.rsplit('.',1)[-1].lower()=='jpg' else value.rsplit('.',1)[-1].lower()
            if extension not in self.formats:self.formats.append(extension)
        return self


class Authorization(Contract):
    goal:str=Field(min_length=1,max_length=8000)
    allowed_assets:list[str]=Field(max_length=100)
    allowed_asset_digests:dict[str,str]=Field(default_factory=dict,max_length=100)
    allowed_operations:list[str]=Field(max_length=100)
    allowed_agent_profiles:list[str]=Field(default_factory=list,max_length=20)
    allowed_network_urls:list[str]=Field(default_factory=list,max_length=16,
        description='Exact public HTTPS URLs that delegated code may read through the host Gateway.')
    operation_implementations:dict[str,str]=Field(default_factory=dict,max_length=100)
    operation_policy_digests:dict[str,str]=Field(default_factory=dict,max_length=100)
    agent_profile_digests:dict[str,str]=Field(default_factory=dict,max_length=20)
    provider_call_budget:dict[str,int]=Field(default_factory=dict,max_length=50)
    cost_budget:float|None=Field(default=None,ge=0)
    model_token_budget:int=Field(default=4000000,ge=1024,le=4000000)
    max_model_calls:int=Field(default=200,ge=1,le=10000,description='本轮模型调用次数上限；独立于 Token 预算。')
    wall_time_budget:int=Field(default=86400,ge=60,le=86400)
    max_tasks:int=Field(default=256,ge=1,le=256)
    max_planning_rounds:int=Field(default=64,ge=1,le=64)
    delivery_targets:list[str]=Field(default_factory=list,max_length=20)
    require_plan_confirmation:bool=False
    review_policy:ReviewPolicy=Field(default_factory=ReviewPolicy)
    delivery_contract:DeliveryContract=Field(default_factory=DeliveryContract)
    content_fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    semantic_policy_version:Literal['media-semantics-v1']='media-semantics-v1'

    @model_validator(mode='after')
    def valid(self):
        from .operations import get
        from .specialist_profiles import get as profile
        for op in self.allowed_operations:
            if op!='script':get(op)
        for name in self.allowed_agent_profiles:profile(name)
        from .agent_runtime_capabilities import validate_public_url
        if len(set(self.allowed_network_urls))!=len(self.allowed_network_urls):raise ValueError('Duplicate authorized network URL')
        for value in self.allowed_network_urls:validate_public_url(value)
        from .operations import REGISTRY,_IMPLEMENTATION_BINDINGS
        definitions=[*REGISTRY.values(),*[item for variants in _IMPLEMENTATION_BINDINGS.values() for item in variants.values()]]
        groups={item.budget_group for item in definitions if item.external_side_effects}
        for name,limit in self.provider_call_budget.items():
            if name not in groups or type(limit)!=int or not 0<=limit<=100:
                raise ValueError('Invalid authorized provider budget')
        if any(not item.strip() for item in self.delivery_targets):raise ValueError('Empty delivery target')
        return self


class DecisionInputBatch(Contract):
    content:str=Field(default='',max_length=4000)
    attachments:list[str]=Field(default_factory=list,max_length=20)
    targets:list[str]=Field(default_factory=list,max_length=64)


class DecisionAnswer(Contract):
    decision_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    snapshot_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    action:Literal['approve','revise','reject']
    external_id:str=Field(default='',max_length=200)
    authorization:Authorization|None=None
    feedback:str=Field(default='',max_length=4000)
    attachments:list[str]=Field(default_factory=list,max_length=20)
    upstream_tasks:list[str]=Field(default_factory=list,max_length=64)
    input_batch:DecisionInputBatch=Field(default_factory=DecisionInputBatch)

    @model_validator(mode='after')
    def feedback_required(self):
        if (not self.input_batch.content and not self.input_batch.attachments and not self.input_batch.targets
                and (self.feedback or self.attachments or self.upstream_tasks)):
            self.input_batch=DecisionInputBatch(content=self.feedback,attachments=list(self.attachments),targets=list(self.upstream_tasks))
        if self.input_batch.content and not self.feedback:self.feedback=self.input_batch.content
        if self.input_batch.attachments and not self.attachments:self.attachments=list(self.input_batch.attachments)
        if self.input_batch.targets and not self.upstream_tasks:self.upstream_tasks=list(self.input_batch.targets)
        if self.action=='revise' and not self.feedback.strip():
            raise ValueError('Revise requires nonempty feedback')
        if self.action!='revise' and (self.attachments or self.upstream_tasks or self.input_batch.content):
            raise ValueError('Only revise can introduce inputs')
        return self


class SubjectSemantics(Contract):
    mode:Literal['subject_cutout','foreground_cutout']
    include_relations:list[Literal['body','clothing','worn','held','carried']]=Field(default_factory=list,max_length=5)
    exclude_relations:list[Literal['held','carried']]=Field(default_factory=list,max_length=2)
    @model_validator(mode='after')
    def defaults(self):
        included=set(self.include_relations);excluded=set(self.exclude_relations)
        if included & excluded:raise ValueError('Subject relations cannot be both included and excluded')
        base={'body','clothing','worn'}|({'held','carried'} if self.mode=='foreground_cutout' else set())
        include=(base|included)-excluded
        exclude=(({'held','carried'} if self.mode=='subject_cutout' else set())|excluded)-included
        self.include_relations=[name for name in ('body','clothing','worn','held','carried') if name in include]
        self.exclude_relations=[name for name in ('held','carried') if name in exclude]
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
    reference_role:str=Field(default='mask',pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    output_role:str=Field(default='mask',pattern=r'^[a-z][a-z0-9_-]{0,39}$')
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


class AgentRuntimeRequirements(Contract):
    requires_gpu:bool=Field(default=False,description='True only when generated code must directly use a GPU or a host-mounted GPU model. GPU tasks use the bwrap code editor; portable CPU tasks prefer the configured OpenSandbox Harness.')
    python_modules:list[str]=Field(default_factory=list,max_length=64,description='Import module names required by the generated code. Select only modules advertised by the chosen code-editor runtime.')
    node_modules:list[str]=Field(default_factory=list,max_length=64,description='Node package names required by generated code, selected from the code-editor runtime capability catalogue.')
    system_tools:list[str]=Field(default_factory=list,max_length=64,description='Executable names required by generated code, selected from the code-editor runtime capability catalogue.')
    network_urls:list[str]=Field(default_factory=list,max_length=16,description='Exact public HTTPS URLs the generated code may read through the host Gateway. Empty means no public network access.')
    native_skills:list[str]=Field(default_factory=list,max_length=25,
        description='Exact image-pinned Native Skill IDs required by this OpenSandbox Harness Task. Empty means no task-specific Native Skill.')
    gateway_operations:list[str]=Field(default_factory=list,max_length=8,
        description='Exact host Gateway operations required by the Native Skill. Provider credentials remain on the host; empty grants no provider tools.')

    @model_validator(mode='after')
    def valid(self):
        token=re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,79}$')
        if (len(set(self.python_modules))!=len(self.python_modules)
                or len(set(self.node_modules))!=len(self.node_modules)
                or len(set(self.system_tools))!=len(self.system_tools)
                or len(set(self.network_urls))!=len(self.network_urls)
                or len(set(self.native_skills))!=len(self.native_skills)
                or len(set(self.gateway_operations))!=len(self.gateway_operations)
                or any(not token.fullmatch(value) for value in (*self.python_modules,*self.node_modules,*self.system_tools,*self.gateway_operations))):
            raise ValueError('Agent runtime requirements must be unique registered capability names')
        from .native_skill_catalog import validate as validate_native_skills,required_gateway_operations
        validate_native_skills(self.native_skills)
        required=set(required_gateway_operations(self.native_skills))
        if not required<=set(self.gateway_operations):
            raise ValueError('Native Skill requires Gateway operations: '+', '.join(sorted(required-set(self.gateway_operations))))
        if self.requires_gpu and (self.native_skills or self.gateway_operations):
            raise ValueError('Native Skills and Gateway operations require the OpenSandbox CPU Harness and cannot be declared for GPU/bwrap')
        from .agent_runtime_capabilities import validate_public_url
        for value in self.network_urls:validate_public_url(value)
        return self


class Task(Contract):
    id:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    type:Literal['operation','script','agent']
    purpose:Literal['business','repair','evidence']='business'
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
    preflight:list[str]=Field(default_factory=list,max_length=20,
        description='Existing workspace script paths beginning with edit/. Not commands, package names, or environment requirements. Leave empty unless an actual preflight file exists.',
        json_schema_extra={'items':{'type':'string','pattern':'^edit/'}})
    config_files:list[str]=Field(default_factory=list,max_length=20,
        description='Existing workspace configuration file paths beginning with edit/. No parent traversal or absolute paths.',
        json_schema_extra={'items':{'type':'string','pattern':'^edit/'}})
    timeout_seconds:int=Field(default=900,ge=10,le=3600)
    retry_policy:Literal['never','safe_once']=Field(default='never',description='业务失败重试策略；确定性本地操作的环境故障由系统自动重试，不需要模型设置。')
    on_failure:Literal['pause','diagnose']=Field(default='pause',description='Use diagnose for model-authored implementations when media_failure_diagnostician is authorized, so bounded repair can resume the original goal.')
    basic_reason:str=Field(default='',max_length=1000)
    semantics:TaskSemantics=Field(default_factory=TaskSemantics)
    runtime_requirements:AgentRuntimeRequirements=Field(default_factory=AgentRuntimeRequirements,
        description='Code-editor execution requirements. Portable CPU is the default; declare GPU, dependencies and exact public URLs explicitly.')
    delivery_stage:bool=Field(default=False,description='True for Tasks whose accepted outputs are eligible for final delivery. A sealed graph requires at least one.')

    @model_validator(mode='after')
    def valid(self):
        if self.type=='agent':
            from .specialist_profiles import get
            profile=get(self.profile)
            if profile.role in ('PLANNER','REVIEWER','DIAGNOSTICIAN'):
                raise ValueError('Control profiles cannot be delegated as business tasks')
            if self.operation or self.implementation or self.argv or self.parameters or self.preflight or self.config_files:
                raise ValueError('Agent profiles do not accept executable policy overrides')
            if self.profile!='editing_implementer' and self.runtime_requirements!=AgentRuntimeRequirements():
                raise ValueError('Only the code editor accepts runtime requirements')
            if not self.output_roles:raise ValueError('Agent task must declare output roles')
            if self.timeout_seconds>profile.timeout_seconds:raise ValueError('Agent timeout exceeds profile')
            if self.runtime_requirements.native_skills and self.timeout_seconds<profile.timeout_seconds:
                raise ValueError('Native Skill Agent timeout must use the profile maximum')
            for role,name in self.output_roles.items():
                if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',role) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}',name):
                    raise ValueError('Invalid Agent output role or filename')
            if self.outputs and set(self.outputs)!=set(self.output_roles.values()):raise ValueError('Agent outputs must match its declared output role filenames')
            declared=set(self.outputs or self.output_roles.values())
            if any(check.file!='*' and check.file not in declared for check in self.acceptance):
                raise ValueError('Agent acceptance file must be a declared output')
            from .specialist_profiles import result_model
            schema=result_model(profile)
            if self.delivery_stage and profile.role in ('REVIEWER','DIAGNOSTICIAN'):
                raise ValueError('Reviewer and diagnostician report Tasks cannot be delivery stages; mark the actual business output Task for delivery')
            if schema:
                if len(self.output_roles)!=1:raise ValueError('Report profiles produce exactly one typed JSON report')
                if any(not name.endswith('.json') for name in self.output_roles.values()):raise ValueError('This Agent profile produces JSON reports only')
                fields=set(schema.model_fields)
                if any(check.kind=='json' and not set(check.required_keys)<=fields for check in self.acceptance):
                    raise ValueError('Acceptance requires fields outside the fixed Agent result schema')
        else:
            if self.profile:raise ValueError('Only agent tasks accept a profile')
            if self.runtime_requirements!=AgentRuntimeRequirements():raise ValueError('Only agent tasks accept runtime requirements')
            if self.type=='script' and self.argv and self.argv[0].startswith('-'):
                raise ValueError('script argv must start with an executable such as python, not an option; no implicit implementation exists')
            from .workflows import Step
            step=Step.model_validate(self.execution_spec())
            self.parameters=step.parameters;self.implementation=step.implementation
        if not self.basic_reason and all(c.kind=='file' for c in self.acceptance):
            raise ValueError('Basic-only acceptance requires a reason')
        if any(c.stage=='final' for c in self.acceptance) and not self.delivery_stage:
            raise ValueError('Final-stage acceptance belongs only on an explicit delivery-stage task')
        if any(ref.repair_candidate for ref in self.inputs.values()) and self.purpose not in ('repair','evidence'):
            raise ValueError('Candidate inputs are read-only and limited to repair/evidence Tasks')
        if self.purpose=='evidence':
            from .operations import get as operation
            if self.type!='operation' or not operation(self.operation,self.implementation).evidence_only or self.delivery_stage:
                raise ValueError('Evidence-only Tasks require a registered collector and cannot be delivery stages')
        mask=self.semantics.mask_repair
        if mask and mask.level=='D':raise ValueError('Ambiguous mask scope requires REQUEST_USER_INPUT before execution')
        if mask and mask.level in ('A','B'):
            from .operations import get as operation
            roles=operation(self.operation,self.implementation).outputs if self.type=='operation' else self.output_roles
            if mask.reference_role not in self.inputs or mask.output_role not in roles:
                raise ValueError('Local mask repair requires declared reference and output mask roles')
        return self

    def execution_spec(self,dependencies=()):
        result={'id':self.id,'kind':self.type,'name':self.name,'goal':self.objective,
                'operation':self.operation,'implementation':self.implementation,
                'inputs':{k:v.model_dump() for k,v in self.inputs.items()},'parameters':self.parameters,
                'output_roles':self.output_roles,'outputs':self.outputs or (list(self.output_roles.values()) if self.type=='agent' else []),
                'argv':self.argv,'preflight':self.preflight,'config_files':self.config_files,
                'timeout':self.timeout_seconds,'retry_safe':self.retry_policy=='safe_once',
                'acceptance':[a.model_dump() for a in self.acceptance],'repairs':[],
                'on_failure':self.on_failure,'basic_reason':self.basic_reason,'depends_on':list(dependencies)}
        if self.type=='agent':result['runtime_requirements']=self.runtime_requirements.model_dump()
        return result


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
    remove_dependencies:list[Dependency]=Field(default_factory=list,max_length=512)
    replacements:dict[str,str]=Field(default_factory=dict,max_length=16,
        description='Explicit old logical Task ID to newly added Task ID. Preserves required goal coverage and rewires affected descendants with new revisions.')
    review_again:dict[str,list[str]]=Field(default_factory=dict,max_length=16,
        description='Review a program-verified candidate again using completed evidence-only Task IDs. Does not rerun media or change acceptance.')
    replan_after:list[str]=Field(default_factory=list,max_length=64)
    sealed:bool=False

    @model_validator(mode='after')
    def bounded_reviews(self):
        if any(not 1<=len(items)<=16 or len(set(items))!=len(items) for items in self.review_again.values()):
            raise ValueError('Each new review requires 1..16 distinct evidence Tasks')
        return self


class ManagerResult(Contract):
    kind:Literal['CONTINUE','APPLY_PLAN','REQUEST_AUTHORIZATION','REQUEST_USER_INPUT','COMPLETE','NEW_RUN_REQUIRED','RESPOND']
    snapshot_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    base_graph_version:int=Field(ge=0)
    authorization_version:int=Field(ge=1)
    input_through_sequence:int=Field(default=0,ge=0)
    proposal:Proposal|None=None
    reason:str=Field(min_length=1,max_length=2000)
    question:str=Field(default='',max_length=2000)
    options:list[str]=Field(default_factory=list,max_length=8)
    authorization:Authorization|None=None
    new_goal:str=Field(default='',max_length=8000)
    delivery_targets:list[str]=Field(default_factory=list,max_length=20)
    review_policy:ReviewPolicy|None=None
    delivery_contract:DeliveryContract|None=None

    @model_validator(mode='after')
    def shape(self):
        requires_proposal=self.kind in ('APPLY_PLAN','COMPLETE')
        if requires_proposal != (self.proposal is not None):raise ValueError('Only APPLY_PLAN and COMPLETE carry a Proposal')
        if self.kind=='CONTINUE' and self.input_through_sequence<=0:raise ValueError('CONTINUE requires a frozen input batch')
        if self.kind=='RESPOND' and (not self.reason.strip() or self.authorization is not None or self.question or self.options or self.new_goal or self.delivery_targets):raise ValueError('RESPOND carries only an answer, never a plan, question, delivery or authorization')
        if (self.review_policy is not None or self.delivery_contract is not None) and self.kind not in ('APPLY_PLAN','COMPLETE'):
            raise ValueError('Review and delivery policy are proposed only with an executable plan')
        if self.proposal and self.proposal.base_graph_version!=self.base_graph_version:raise ValueError('Proposal version differs from result envelope')
        if self.kind=='COMPLETE' and not self.proposal.sealed:raise ValueError('COMPLETE requires a sealed Proposal and independent final review')
        if self.kind=='REQUEST_AUTHORIZATION' and self.authorization is None:raise ValueError('Authorization request requires the complete proposed scope')
        if self.kind=='REQUEST_USER_INPUT' and not self.question.strip():raise ValueError('User input request requires an explicit question')
        if self.kind=='NEW_RUN_REQUIRED' and not self.new_goal.strip():raise ValueError('Successor request requires an explicit new goal')
        if self.kind=='NEW_RUN_REQUIRED' and (self.authorization is None or self.authorization.goal!=self.new_goal):
            raise ValueError('Successor request requires its complete proposed authorization')
        return self


def _upgrade_schema(store,db):
    work_item_additions={'infrastructure_interruptions':'INTEGER NOT NULL DEFAULT 0',
                        'poll_count':'INTEGER NOT NULL DEFAULT 0','wait_recorded_at':'REAL'}
    additions={
        'runs':{
            'reconcile_error_signature':"TEXT NOT NULL DEFAULT ''",
            'reconcile_error_count':'INTEGER NOT NULL DEFAULT 0',
        },
        'run_tasks':work_item_additions,
        'control_jobs':work_item_additions,
        'task_results':{'error_json':"TEXT NOT NULL DEFAULT '{}'"},
        'workflow_plan_drafts':{'last_error_json':"TEXT NOT NULL DEFAULT '[]'"},
    }
    for table,definitions in additions.items():
        existing={row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name,declaration in definitions.items():
            if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

    expected=['run_id','upstream_task_id','downstream_task_id','created_graph_version']
    if store.dsn:
        rows=db.execute('''SELECT kcu.column_name AS name,tc.constraint_name AS constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_schema=kcu.constraint_schema AND tc.constraint_name=kcu.constraint_name
            WHERE tc.table_schema=current_schema() AND tc.table_name='workflow_dependencies'
              AND tc.constraint_type='PRIMARY KEY' ORDER BY kcu.ordinal_position''').fetchall()
        current=[row['name'] for row in rows]
        if current!=expected:
            constraint=rows[0]['constraint_name'] if rows else ''
            if constraint!='workflow_dependencies_pkey':raise ValueError('Unexpected workflow dependency primary key')
            db.execute('ALTER TABLE workflow_dependencies DROP CONSTRAINT workflow_dependencies_pkey')
            db.execute('ALTER TABLE workflow_dependencies ADD PRIMARY KEY(run_id,upstream_task_id,downstream_task_id,created_graph_version)')
    else:
        current=[row['name'] for row in sorted(db.execute('PRAGMA table_info(workflow_dependencies)'),key=lambda row:row['pk']) if row['pk']]
        if current!=expected:
            db.execute('ALTER TABLE workflow_dependencies RENAME TO workflow_dependencies_legacy')
            db.execute('''CREATE TABLE workflow_dependencies(
                run_id TEXT NOT NULL,upstream_task_id TEXT NOT NULL,downstream_task_id TEXT NOT NULL,
                created_graph_version INTEGER NOT NULL,retired_graph_version INTEGER,
                PRIMARY KEY(run_id,upstream_task_id,downstream_task_id,created_graph_version))''')
            db.execute('''INSERT INTO workflow_dependencies
                SELECT run_id,upstream_task_id,downstream_task_id,created_graph_version,retired_graph_version
                FROM workflow_dependencies_legacy''')
            db.execute('DROP TABLE workflow_dependencies_legacy')


def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS session_messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,run_id TEXT,role TEXT NOT NULL,content TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS session_message_order ON session_messages(session_id,created_at,id);
        CREATE TABLE IF NOT EXISTS conversation_summaries(
          session_id TEXT PRIMARY KEY,through_created_at REAL NOT NULL,through_message_id TEXT NOT NULL,
          source_digest TEXT NOT NULL,summary_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'committed',error_code TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS conversation_summary_jobs(
          session_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,target_created_at REAL NOT NULL,target_message_id TEXT NOT NULL,
          source_digest TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
          next_run_at REAL NOT NULL DEFAULT 0,error_code TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL,session_id TEXT NOT NULL,parent_run_id TEXT,
          scope_confirmed INTEGER NOT NULL DEFAULT 1,input_sequence INTEGER NOT NULL DEFAULT 0,input_barrier_sequence INTEGER,input_barrier_was_sealed INTEGER NOT NULL DEFAULT 0,
          goal TEXT NOT NULL,status TEXT NOT NULL,graph_version INTEGER NOT NULL DEFAULT 0,
          sealed INTEGER NOT NULL DEFAULT 0,authorization_json TEXT NOT NULL,
          authorization_version INTEGER NOT NULL DEFAULT 1,replan_after_json TEXT NOT NULL DEFAULT '[]',
          repair_context_json TEXT NOT NULL DEFAULT '{}',
          next_reconcile_at REAL NOT NULL,reconcile_owner TEXT NOT NULL DEFAULT '',
          reconcile_until REAL NOT NULL DEFAULT 0,reconcile_generation INTEGER NOT NULL DEFAULT 0,
          reconcile_error_signature TEXT NOT NULL DEFAULT '',reconcile_error_count INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,terminal_reason TEXT NOT NULL DEFAULT '',
          delivery_json TEXT NOT NULL DEFAULT '[]',delivered_at REAL);
        CREATE TABLE IF NOT EXISTS run_authorizations(run_id TEXT NOT NULL,version INTEGER NOT NULL,authorization_json TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(run_id,version));
        CREATE TABLE IF NOT EXISTS workflow_asset_reuse(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,
          revision INTEGER NOT NULL,path TEXT NOT NULL,artifact_id TEXT NOT NULL,sha256 TEXT NOT NULL,
          asset_id TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS workflow_due ON runs(next_reconcile_at,status);
        CREATE TABLE IF NOT EXISTS run_tasks(
          run_id TEXT NOT NULL,id TEXT NOT NULL,type TEXT NOT NULL,purpose TEXT NOT NULL DEFAULT 'business',authorization_version INTEGER NOT NULL DEFAULT 1,spec_json TEXT NOT NULL,
          profile_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL,selected INTEGER NOT NULL DEFAULT 1,phase TEXT NOT NULL DEFAULT 'local',
          created_graph_version INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
          recovery_count INTEGER NOT NULL DEFAULT 0,poll_count INTEGER NOT NULL DEFAULT 0,wait_recorded_at REAL,infrastructure_interruptions INTEGER NOT NULL DEFAULT 0,review_again_count INTEGER NOT NULL DEFAULT 0,repair_count INTEGER NOT NULL DEFAULT 0,user_revision_count INTEGER NOT NULL DEFAULT 0,
          repair_context_json TEXT NOT NULL DEFAULT '{}',
          attempts INTEGER NOT NULL DEFAULT 0,next_run_at REAL NOT NULL DEFAULT 0,
          wait_reason TEXT NOT NULL DEFAULT '',candidate_result_json TEXT NOT NULL DEFAULT '[]',
          accepted_result_json TEXT NOT NULL DEFAULT '[]',quality_json TEXT NOT NULL DEFAULT '{}',
          bindings_json TEXT NOT NULL DEFAULT '{}',external_id TEXT NOT NULL DEFAULT '',
          error_code TEXT NOT NULL DEFAULT '',error_detail TEXT NOT NULL DEFAULT '',
          lease_id TEXT NOT NULL DEFAULT '',lease_generation INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS control_jobs(
          run_id TEXT NOT NULL,id TEXT NOT NULL,type TEXT NOT NULL DEFAULT 'agent',purpose TEXT NOT NULL DEFAULT 'business',authorization_version INTEGER NOT NULL DEFAULT 1,control_kind TEXT NOT NULL,snapshot_json TEXT NOT NULL,snapshot_digest TEXT NOT NULL,spec_json TEXT NOT NULL,
          profile_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL,selected INTEGER NOT NULL DEFAULT 1,phase TEXT NOT NULL DEFAULT 'local',
          created_graph_version INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
          recovery_count INTEGER NOT NULL DEFAULT 0,poll_count INTEGER NOT NULL DEFAULT 0,wait_recorded_at REAL,infrastructure_interruptions INTEGER NOT NULL DEFAULT 0,review_again_count INTEGER NOT NULL DEFAULT 0,repair_count INTEGER NOT NULL DEFAULT 0,user_revision_count INTEGER NOT NULL DEFAULT 0,
          repair_context_json TEXT NOT NULL DEFAULT '{}',
          attempts INTEGER NOT NULL DEFAULT 0,next_run_at REAL NOT NULL DEFAULT 0,
          wait_reason TEXT NOT NULL DEFAULT '',candidate_result_json TEXT NOT NULL DEFAULT '[]',
          accepted_result_json TEXT NOT NULL DEFAULT '[]',quality_json TEXT NOT NULL DEFAULT '{}',
          bindings_json TEXT NOT NULL DEFAULT '{}',external_id TEXT NOT NULL DEFAULT '',
          error_code TEXT NOT NULL DEFAULT '',error_detail TEXT NOT NULL DEFAULT '',
          lease_id TEXT NOT NULL DEFAULT '',lease_generation INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS workflow_task_revisions(
          run_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,spec_json TEXT NOT NULL,
          created_graph_version INTEGER NOT NULL,repair_root INTEGER NOT NULL DEFAULT 0,
          supersedes_revision INTEGER,reason TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,
          PRIMARY KEY(run_id,task_id,revision));
        CREATE TABLE IF NOT EXISTS workflow_plan_tasks(
          run_id TEXT NOT NULL,graph_version INTEGER NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,
          selected_at REAL NOT NULL,PRIMARY KEY(run_id,graph_version,task_id));
        CREATE INDEX IF NOT EXISTS workflow_ready ON run_tasks(status,next_run_at);
        CREATE TABLE IF NOT EXISTS workflow_dependencies(
          run_id TEXT NOT NULL,upstream_task_id TEXT NOT NULL,downstream_task_id TEXT NOT NULL,
          created_graph_version INTEGER NOT NULL,retired_graph_version INTEGER,
          PRIMARY KEY(run_id,upstream_task_id,downstream_task_id,created_graph_version));
        CREATE TABLE IF NOT EXISTS run_task_replacements(run_id TEXT NOT NULL,old_task_id TEXT NOT NULL,new_task_id TEXT NOT NULL,graph_version INTEGER NOT NULL,source TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(run_id,old_task_id));
        CREATE TABLE IF NOT EXISTS planner_runs(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT NOT NULL,base_graph_version INTEGER NOT NULL,
          authorization_version INTEGER NOT NULL,trigger_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,
          snapshot_digest TEXT NOT NULL,status TEXT NOT NULL,proposal_json TEXT NOT NULL DEFAULT '{}',
          lease_id TEXT NOT NULL DEFAULT '',lease_generation INTEGER NOT NULL DEFAULT 0,
          proposal_id TEXT,applied_graph_version INTEGER,attempts INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL,error_code TEXT NOT NULL DEFAULT '',
          UNIQUE(run_id,task_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_planner ON planner_runs(run_id)
          WHERE status IN ('pending','running','staged');
        CREATE UNIQUE INDEX IF NOT EXISTS unique_graph_proposal ON planner_runs(run_id,proposal_id)
          WHERE proposal_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS workflow_commands(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,
          payload_json TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,applied_at REAL,
          error_code TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS task_results(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,
          lease_id TEXT NOT NULL,lease_generation INTEGER NOT NULL,outcome TEXT NOT NULL,result_json TEXT NOT NULL,
          artifact_manifest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'staged',error_json TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,committed_at REAL,
          UNIQUE(run_id,task_id,task_revision,lease_generation));
        CREATE TABLE IF NOT EXISTS workflow_decisions(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,kind TEXT NOT NULL,graph_version INTEGER NOT NULL,
          authorization_version INTEGER NOT NULL,snapshot_digest TEXT NOT NULL,request_json TEXT NOT NULL,
          answer_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_reviews(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT NOT NULL,purpose TEXT NOT NULL,
          subject_task TEXT NOT NULL DEFAULT '',graph_version INTEGER NOT NULL,review_identity TEXT NOT NULL,snapshot_json TEXT NOT NULL,
          snapshot_digest TEXT NOT NULL,status TEXT NOT NULL,decision_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS unique_review_content ON workflow_reviews(run_id,purpose,review_identity);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_graph_review ON workflow_reviews(run_id,purpose,subject_task)
          WHERE status IN ('pending','running','staged');
        CREATE TABLE IF NOT EXISTS workflow_graph_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,kind TEXT NOT NULL,
          graph_version INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS specialist_attempt_profiles(attempt_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,work_id TEXT NOT NULL,profile_id TEXT NOT NULL,harness_id TEXT NOT NULL,policy_digest TEXT NOT NULL,model TEXT NOT NULL,protocol TEXT NOT NULL,model_config_digest TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS native_task_sessions(
          run_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,
          attempt_id TEXT NOT NULL,configuration_digest TEXT NOT NULL,input_digest TEXT NOT NULL,
          session_id TEXT NOT NULL,output_directory TEXT NOT NULL,manifest_json TEXT NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(run_id,task_id,revision));
        CREATE TABLE IF NOT EXISTS workflow_model_reservations(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
          reserved_tokens INTEGER NOT NULL,used_tokens INTEGER,state TEXT NOT NULL,created_at REAL NOT NULL,
          input_reserved_tokens INTEGER,output_reserved_tokens INTEGER,
          input_settled_tokens INTEGER,output_settled_tokens INTEGER);
        CREATE TABLE IF NOT EXISTS workflow_plan_drafts(
          planning_request_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,snapshot_digest TEXT NOT NULL,
          base_graph_version INTEGER NOT NULL,authorization_version INTEGER NOT NULL,
          input_through_sequence INTEGER NOT NULL,schema_version TEXT NOT NULL,
          status TEXT NOT NULL,draft_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
          last_error_signature TEXT NOT NULL DEFAULT '',last_error_json TEXT NOT NULL DEFAULT '[]',repeated_error_count INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_orchestrators(
          id TEXT PRIMARY KEY,release TEXT NOT NULL,heartbeat REAL NOT NULL,draining INTEGER NOT NULL DEFAULT 0,
          reconciles INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,last_duration REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS workflow_reconcile_failures(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,reconcile_generation INTEGER NOT NULL,
          exception_type TEXT NOT NULL,error_code TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_inspections(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
          reference TEXT NOT NULL,snapshot_digest TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_read_cache(
          run_id TEXT NOT NULL,task_id TEXT NOT NULL,snapshot_digest TEXT NOT NULL,
          kind TEXT NOT NULL,query_digest TEXT NOT NULL,request_json TEXT NOT NULL,result_json TEXT NOT NULL,
          source_attempt TEXT NOT NULL,created_at REAL NOT NULL,
          PRIMARY KEY(run_id,task_id,snapshot_digest,kind,query_digest));
        CREATE TABLE IF NOT EXISTS workflow_agent_outputs(
          lease_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT NOT NULL,
          generation INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL);
        ''')
    with store.connect() as db:
        _upgrade_schema(store,db)
        from .run_inputs import initialize as initialize_inputs
        initialize_inputs(db)
        # PostgreSQL cannot CREATE OR REPLACE a SELECT-* view when a migrated
        # base table inserts columns before control_kind. Views carry no data,
        # so recreate this projection after the transactional table upgrade.
        task_fields=[row['name'] for row in db.execute('PRAGMA table_info(run_tasks)')]
        control_fields={row['name'] for row in db.execute('PRAGMA table_info(control_jobs)')}
        expected_control_fields=set(task_fields)|{'control_kind','snapshot_json','snapshot_digest'}
        if control_fields!=expected_control_fields:
            raise ValueError('run_tasks and control_jobs columns diverged: missing from control_jobs='
                             +repr(sorted(expected_control_fields-control_fields))
                             +'; unexpected in control_jobs='+repr(sorted(control_fields-expected_control_fields)))
        db.execute('DROP VIEW IF EXISTS run_work_items')
        view_ddl='CREATE OR REPLACE VIEW ' if store.dsn else 'CREATE VIEW IF NOT EXISTS '
        fields=','.join(task_fields)
        db.execute(view_ddl+"run_work_items AS SELECT "+fields+",'' AS control_kind FROM run_tasks UNION ALL SELECT "+fields+",control_kind FROM control_jobs")
        db.execute(view_ddl+"""execution_subjects AS
            SELECT id,project,status,created,updated,'agent' AS kind FROM agent_turns WHERE id NOT IN (SELECT id FROM runs)
            UNION ALL SELECT id,project,status,created,updated,'manual' AS kind FROM jobs
            UNION ALL SELECT id,project_id AS project,
              CASE WHEN status IN ('ACTIVE','WAITING_USER') THEN 'waiting' ELSE lower(status) END AS status,
              created_at AS created,updated_at AS updated,'run' AS kind FROM runs""")
    store._dynamic_schema=True


def get(store,identifier):
    if not hasattr(store,'_dynamic_schema'):
        store._dynamic_schema=bool(store.query("SELECT name FROM sqlite_master WHERE name='runs'"))
    if not store._dynamic_schema:return None
    rows=store.query('SELECT * FROM runs WHERE id=?',(identifier,))
    if not rows:return None
    run=rows[0]
    for field in ('authorization','replan_after','repair_context','delivery'):run[field]=json.loads(run.pop(field+'_json'))
    run['sealed']=bool(run['sealed'])
    run['tasks']=store.query('SELECT * FROM run_tasks WHERE run_id=? ORDER BY created_graph_version,created_at,id',(run['id'],))
    run['control_jobs']=store.query('SELECT * FROM control_jobs WHERE run_id=? ORDER BY created_at,id',(run['id'],))
    # Retired business nodes remain part of the public/audit snapshot even
    # though work_items() intentionally excludes them from dispatch.
    for task in run['tasks']+run['control_jobs']:
        for field in ('spec','profile','candidate_result','accepted_result','quality','bindings','repair_context'):
            task[field]=json.loads(task.pop(field+'_json'))
    run['dependencies']=store.query('SELECT upstream_task_id,downstream_task_id FROM workflow_dependencies WHERE run_id=? AND retired_graph_version IS NULL',(run['id'],))
    run['drain_set']=store.query('SELECT task_id,revision,barrier_sequence FROM run_input_drain WHERE run_id=?',(run['id'],))
    run['activity']=activity(run)
    return run


def event(store,run,kind,payload=None):
    store.execute('INSERT INTO workflow_graph_events(run_id,kind,graph_version,payload_json,created_at) VALUES (?,?,?,?,?)',
                  (run['id'],kind,run['graph_version'],encode(payload or {}),store.now()))
    from .agent_store import event as agent_event
    from .telemetry import emit
    agent_event(store,run['id'],'workflow_graph',{'run_id':run['id'],'graph_version':run['graph_version'],'kind':kind,**(payload or {})})
    fields={'task_id':run['id'],'run_id':run['id'],'graph_version':run['graph_version'],
            'sealed':run['sealed'],'project_id':run['project_id']}
    for key in ('proposal_id','error_code','revision','outcome','input_id','input_sequence','through_sequence','next_barrier_sequence','result_kind','source','review_identity','previous_review_id','successor_run_id'):
        if key in (payload or {}) and payload[key]!='':fields[key]=payload[key]
    if payload and payload.get('status'):fields['new_state']=payload['status']
    if payload and payload.get('reason') and re.fullmatch(r'[a-z_]{1,80}',str(payload['reason'])):fields['reason']=payload['reason']
    if payload and payload.get('task_id'):fields['step_id']=payload['task_id']
    store.notify_after_commit(lambda:emit('workflow.'+kind,**fields))


def content_fingerprint(store,project):
    from .plan_readiness import fingerprint
    return fingerprint(store,project)


def asset_digest(asset):
    return digest({key:asset[key] for key in ('id','name','filename','metadata')})


def create(store,project,identifier,raw,*,session_id=None,parent_run_id=None,scope_confirmed=True,record_message=True,
           model_configuration=None,role_model_configurations=None):
    from .workflow_planning import bind_scope
    authorization=bind_scope(store,raw) if scope_confirmed else Authorization.model_validate(raw)
    identifier=identifier or uuid.uuid4().hex
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise ValueError('Unknown project')
        existing=db.execute('''SELECT id,project_id,session_id,parent_run_id,scope_confirmed,authorization_json
            FROM runs WHERE id=?''',(identifier,)).fetchone()
        if existing:
            if existing['project_id']!=project:raise ValueError('Run project is immutable')
            if existing['parent_run_id']!=parent_run_id:raise ValueError('Run parent is immutable')
            if session_id is not None and existing['session_id']!=session_id:raise ValueError('Run Session is immutable')
            if bool(existing['scope_confirmed'])!=bool(scope_confirmed):raise ValueError('Run scope confirmation is immutable')
            if existing['authorization_json']!=encode(authorization.model_dump()):raise ValueError('Run authorization is immutable')
            if model_configuration is not None:
                from .agent_profiles import for_task
                if for_task(store,identifier).digest!=model_configuration.digest:raise ValueError('Run model configuration is immutable')
            return get(store,existing['id'])
        store.admit_tasks(project,1,include_open_runs=True)
        parent=get(store,parent_run_id) if parent_run_id else None
        if parent_run_id and (not parent or parent['project_id']!=project):raise ValueError('Foreign parent Run')
        session_id=session_id or (parent['session_id'] if parent else uuid.uuid4().hex)
        session=db.execute('SELECT project_id FROM sessions WHERE id=?',(session_id,)).fetchone()
        if session and session['project_id']!=project:raise ValueError('Foreign Session')
        if parent and session_id!=parent['session_id']:raise ValueError('Successor must stay in the same Session')
        if db.execute("SELECT id FROM runs WHERE session_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(session_id,)).fetchone():
            raise ValueError('Session already has an active Run')
        owned={r['id'] for r in db.execute('SELECT id FROM assets WHERE project=?',(project,))}
        if not set(authorization.allowed_assets)<=owned:raise ValueError('Asset outside project')
        if authorization.content_fingerprint!=content_fingerprint(store,project):raise ValueError('Approved content has changed')
        now=store.now()
        db.execute('INSERT OR IGNORE INTO sessions(id,project_id,created_at) VALUES (?,?,?)',(session_id,project,now))
        db.execute("INSERT INTO runs(id,project_id,session_id,parent_run_id,scope_confirmed,goal,status,authorization_json,next_reconcile_at,created_at,updated_at) VALUES (?,?,?,?,?,?,'ACTIVE',?,?,?,?)",
            (identifier,project,session_id,parent_run_id,int(scope_confirmed),authorization.goal,encode(authorization.model_dump()),now,now,now))
        db.execute('INSERT INTO run_authorizations VALUES (?,?,?,?)',(identifier,1,encode(authorization.model_dump()),now))
        from .task_lifecycle import ensure
        ensure(db,identifier,now)
        db.execute('UPDATE execution_limits SET deadline_at=?,budget_seconds=? WHERE task_id=?',
                   (now+authorization.wall_time_budget,authorization.wall_time_budget,identifier))
        from .agent_profiles import freeze,for_task
        if model_configuration is None and parent_run_id:model_configuration=for_task(store,parent_run_id)
        if not db.execute('SELECT task_id FROM agent_run_profiles WHERE task_id=?',(identifier,)).fetchone():
            freeze(db,identifier,project,now,parent_run_id,configuration=model_configuration,role_profiles=role_model_configurations)
        from .task_usage import attach
        attach(db,identifier,project,now)
        from .observation_schema import register
        register(db,identifier,project,now)
        if record_message:
            db.execute('INSERT INTO session_messages(id,session_id,run_id,role,content,created_at) VALUES (?,?,?,?,?,?)',
                (uuid.uuid4().hex,session_id,identifier,'user',authorization.goal,now))
        run=get(store,identifier)
        if record_message:
            from .conversation_memory import message_recorded
            message_recorded(store,run)
        event(store,run,'created');return run


def bind_authorization(raw,*,implementations=None,policies=None):
    authorization=Authorization.model_validate(raw)
    from .operations import get as operation
    from .operation_lifecycle import for_operation
    from .specialist_profiles import authorization_snapshot
    if implementations is None:
        implementations={name:for_operation(operation(name)).id for name in authorization.allowed_operations if name!='script'}
    else:
        implementations={name:for_operation(operation(name,implementations[name])).id
                         for name in authorization.allowed_operations if name!='script'}
    profiles={name:digest(authorization_snapshot(name)) for name in authorization.allowed_agent_profiles}
    if any(implementations.get(name)!=value for name,value in authorization.operation_implementations.items()):
        raise ValueError('Requested operation implementation is no longer current')
    if any(profiles.get(name)!=value for name,value in authorization.agent_profile_digests.items()):
        raise ValueError('Requested Agent policy is no longer current')
    from .provider_metadata import metadata
    policies={name:policies[name] if policies is not None else digest(metadata(operation(name,implementation))) for name,implementation in implementations.items()}
    if any(policies.get(name)!=value for name,value in authorization.operation_policy_digests.items()):
        raise ValueError('Operation side-effect policy changed; renewed authorization required')
    authorization.operation_policy_digests=policies
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
            if (previous['run_id'],previous['actor'],previous['action'],previous['payload_json'])!=(run['id'],actor,action,encode(payload)):
                raise ValueError('Command identity was reused with different content')
            return dict(previous)
        error='workflow_terminal' if run['status'] in RUN_TERMINAL else 'cancellation_in_progress' if run['status']=='CANCELLING' and action!='cancel' else ''
        db.execute('''INSERT INTO workflow_commands(id,run_id,actor,action,payload_json,status,created_at,applied_at,error_code)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (request_id,run['id'],actor,action,encode(payload),'rejected' if error else 'pending',store.now(),store.now() if error else None,error))
        db.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        return {'id':request_id,'status':'rejected' if error else 'pending','error_code':error}


class ProposalFieldError(ValueError):
    def __init__(self,path,message):
        self.path=path;self.code='frozen_field_change'
        super().__init__(message)


def revision_mutable_fields(task_type,user=False):
    fields={'script':['argv'],'operation':['parameters'],'agent':[]}[task_type]
    user_fields=['name','objective','inputs','acceptance','semantics']
    if task_type=='agent':user_fields+=['outputs','output_roles']
    return fields+(user_fields if user else [])


def automatic_revision_retry_safe(task_type,operation_id=None,implementation=None):
    """Retry-safety gate only; scope, frozen fields and budgets remain separate."""
    if task_type!='operation':return True
    from .operations import get as operation
    return operation(operation_id,implementation).retry_safe


def revision_budget_remaining(task,user=False):
    """One budget rule for immutable revisions, replacements and model tools."""
    return max(0,(5 if user else 2)-task.get('user_revision_count' if user else 'repair_count',0))


def validate_operation_input_constraints(task,specs,assets):
    if task.type!='operation':return
    from .operations import get as operation
    constraints=operation(task.operation,task.implementation).public().get('constraints') or {}
    if constraints.get('reference_size_policy')!='match_output':return
    reference=task.inputs.get('source');target=(task.parameters.get('width'),task.parameters.get('height'))
    actual=None
    if reference and reference.asset:
        metadata=assets[reference.asset]['metadata'];actual=(metadata.get('width'),metadata.get('height'))
    elif reference and reference.step in specs:
        parent=specs[reference.step]
        sizes={(rule.width,rule.height) for rule in parent.acceptance
               if rule.kind=='image' and rule.width is not None and rule.height is not None}
        if len(sizes)==1:actual=next(iter(sizes))
    if None in target or actual!=target:
        raise ProposalFieldError('tasks.'+task.id+'.inputs.source',
            'The selected Provider requires its reference image to match the requested output size. Add an accepted preprocessing Task with explicit '
            +str(target[0])+'x'+str(target[1])+' image acceptance and bind that output as source.')


def validate_delivery_dependencies(task,specs):
    if not task.delivery_stage:return
    from .specialist_profiles import get as profile
    for role,reference in task.inputs.items():
        parent=specs.get(reference.step) if reference.step else None
        if parent and parent.type=='agent' and profile(parent.profile).role in ('REVIEWER','DIAGNOSTICIAN'):
            raise ProposalFieldError('tasks.'+task.id+'.inputs.'+role,
                'Reviewer and diagnostician reports are evidence, not deliverable inputs. Mark the real accepted producer with final-stage business acceptance instead of adding a pass-through delivery Agent.')


def validate_proposal(store,run,raw,repair_scope=None,user_revision_scope=None):
    proposal=Proposal.model_validate(raw)
    if run['sealed'] or run['status'] not in ('ACTIVE',):raise ValueError('Workflow cannot accept proposals')
    if proposal.base_graph_version!=run['graph_version']:raise ValueError('Stale graph version')
    authorization=Authorization.model_validate(run['authorization'])
    owned_assets={asset['id']:asset for asset in store.assets(run['project_id'])}
    known_assets=set(owned_assets)
    existing={t['id']:t for t in active_tasks(run)}
    added={task.id:task for task in proposal.add_tasks}
    revised={task.id:task for task in proposal.revise_tasks}
    if (user_revision_scope is None and repair_scope in existing
            and existing[repair_scope]['review_again_count']>=CANDIDATE_REVIEW_LIMIT
            and repair_scope not in revised and repair_scope not in proposal.replacements
            and any(task.purpose=='evidence' for task in added.values())):
        raise ValueError('Candidate evidence review budget exhausted; further automatic evidence cannot be reviewed')
    for subject,evidence_ids in proposal.review_again.items():
        if subject not in existing or subject in revised or subject in proposal.replacements:raise ValueError('Review subject must retain its current candidate revision')
        row=existing[subject]
        if row['review_again_count']>=CANDIDATE_REVIEW_LIMIT:raise ValueError('Candidate evidence review budget exhausted')
        if not row['quality'].get('technical_valid') or not row['quality'].get('passed') or not row['candidate_result']:
            raise ValueError('New evidence cannot waive a program acceptance failure')
        if user_revision_scope is not None:
            if subject not in user_revision_scope:raise ValueError('Review subject outside user feedback scope')
        elif repair_scope not in ('',subject):raise ValueError('Review requires a frozen repair trigger')
        if store.query("SELECT id FROM workflow_reviews WHERE run_id=? AND subject_task=? AND status IN ('pending','running','staged')",(run['id'],subject)):
            raise ValueError('A review for this candidate is still active')
        for identifier in evidence_ids:
            evidence=existing.get(identifier)
            if not evidence or evidence['status']!='succeeded' or evidence['spec']['purpose']!='evidence' or not evidence['quality'].get('verified_evidence'):
                raise ValueError('Only accepted registered evidence can support a new review')
            from .review_evidence import relationship
            if not relationship(row,evidence):
                raise ValueError('Evidence belongs to another candidate revision or different frozen input bytes')
    if len(added)!=len(proposal.add_tasks) or set(added)&{t['id'] for t in run['tasks']} or set(added)&{t['id'] for t in run['control_jobs']}:raise ValueError('Task IDs must be unique for the whole workflow')
    if len(revised)!=len(proposal.revise_tasks) or not set(revised)<=set(existing) or set(revised)&set(added):
        raise ValueError('Task revisions must name unique existing logical tasks')
    if len(existing)+len(added)>authorization.max_tasks:raise AuthorizationRequired('Task budget exhausted')
    edges={(r['upstream_task_id'],r['downstream_task_id']) for r in run['dependencies']}
    replacements=proposal.replacements
    if not set(replacements)<=set(existing) or not set(replacements.values())<=set(added) or len(set(replacements.values()))!=len(replacements):
        raise ValueError('Replacement must map current logical Tasks to distinct new Tasks')
    if set(replacements)&set(revised):raise ValueError('Task cannot be revised and replaced together')
    if replacements and repair_scope is None and user_revision_scope is None:raise ValueError('Replacement requires a frozen repair trigger')
    for old,new in replacements.items():
        current=Task.model_validate(existing[old]['spec']);replacement=added[new]
        row=existing[old]
        if user_revision_scope is not None and old not in user_revision_scope:raise ValueError('Replacement outside user revision scope')
        if any(getattr(current,key)!=getattr(replacement,key) for key in ('objective','acceptance','semantics','delivery_stage','runtime_requirements')):
            raise ValueError('Replacement cannot remove or weaken the original requirement')
        if user_revision_scope is None:
            if repair_scope not in ('',old):raise ValueError('Automatic replacement outside failed Task')
            from .repair_inputs import source_assets
            original_specs={identifier:Task.model_validate(row['spec']) for identifier,row in existing.items()}
            proposed_specs={**original_specs,**added,**revised}
            if not source_assets(store,run,replacement.inputs,proposed_specs)<=source_assets(store,run,current.inputs,original_specs):
                raise ValueError('Automatic replacement cannot introduce unrelated source inputs')
            from .operations import get as operation,active_output_roles
            roles=lambda item:set(active_output_roles(operation(item.operation,item.implementation).public(),item.parameters)) if item.type=='operation' else set(item.output_roles)
            missing=roles(current)-roles(replacement)
            if missing:raise ValueError('Replacement lost a required output role: '+', '.join(sorted(missing)))
        if not revision_budget_remaining(row,user_revision_scope is not None):raise ValueError('Replacement cannot reset the repair budget')
        if user_revision_scope is None and all(getattr(current,key)==getattr(replacement,key) for key in
                ('type','profile','operation','implementation','inputs','parameters','argv','outputs','output_roles')):
            raise ValueError('Automatic replacement must change the failed implementation or use new evidence')
        if row['status'] not in ('paused','failed','succeeded','ready','blocked'):raise ValueError('Replacement must wait for the Task to drain')
    for edge in proposal.remove_dependencies:
        if user_revision_scope is None or edge.downstream not in revised or (edge.upstream,edge.downstream) not in edges:
            raise ValueError('Dependency removal requires an explicit user revision')
        edges.remove((edge.upstream,edge.downstream))
    edges={(replacements.get(parent,parent),replacements.get(child,child)) for parent,child in edges}
    for edge in proposal.add_dependencies:
        if edge.upstream in replacements or edge.downstream in replacements:
            raise ValueError('New dependencies must reference the replacement Task, not a retired Task')
        if edge.downstream not in added and not (user_revision_scope is not None and edge.downstream in revised):raise ValueError('Dependencies of existing tasks are immutable')
        if edge.upstream not in existing and edge.upstream not in added:raise ValueError('Unknown dependency')
        if (edge.upstream,edge.downstream) in edges:raise ValueError('Duplicate dependency')
        edges.add((edge.upstream,edge.downstream))
    remaining=(set(existing)-set(replacements))|set(added);order=[]
    while remaining:
        ready=sorted(n for n in remaining if not any(child==n and parent in remaining for parent,child in edges))
        if not ready:raise ValueError('Dependency cycle')
        order.extend(ready);remaining.difference_update(ready)
    from .operations import get as operation
    if revised:
        if repair_scope is None and user_revision_scope is None:raise ValueError('Task revisions require a frozen repair trigger')
        allowed=set(existing) if repair_scope=='' else set()
        pending=[repair_scope] if repair_scope else []
        while pending:
            current_id=pending.pop()
            if current_id in allowed:continue
            allowed.add(current_id);pending.extend(upstream for upstream,downstream in edges if downstream==current_id)
        if user_revision_scope is not None:allowed=set(user_revision_scope)
        if not set(revised)<=allowed:raise ValueError('Repair revisions are outside the frozen affected scope')
    specs={key:Task.model_validate(t['spec']) for key,t in existing.items() if key not in replacements}
    for identifier,replacement in revised.items():
        current=specs[identifier];row=existing[identifier]
        if row['status'] not in (('paused','succeeded','ready','blocked','failed') if user_revision_scope is not None else ('paused','succeeded')):
            raise ValueError('Only a failed task or accepted dependency ancestor can be revised')
        if row['lease_id']:
            active=store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',(row['lease_id'],store.now()))
            cleaning=store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(row['lease_id'],))
            if active or cleaning:raise ValueError('Task revision must wait for execution and cleanup to finish')
        if not revision_budget_remaining(row,user_revision_scope is not None):raise ValueError('Task revision budget exhausted')
        frozen=('id','type','name','objective','profile','operation','implementation','inputs','output_roles','acceptance','semantics','delivery_stage','runtime_requirements',
                'outputs','preflight','config_files','timeout_seconds','retry_policy','on_failure','basic_reason')
        mutable=revision_mutable_fields(current.type,user_revision_scope is not None)
        frozen=tuple(name for name in frozen if name not in mutable)
        if user_revision_scope is None and (
                any(replacement.inputs.get(name)!=ref for name,ref in current.inputs.items()) or
                any(name not in current.inputs and not ref.repair_candidate for name,ref in replacement.inputs.items())):
            raise ValueError('Automatic revision cannot change original source inputs')
        frozen=tuple(name for name in frozen if name!='inputs')
        changed=[name for name in frozen if getattr(current,name)!=getattr(replacement,name)]
        if changed:
            index=next(i for i,t in enumerate(proposal.revise_tasks) if t.id==identifier)
            raise ProposalFieldError('revise_tasks.'+str(index)+'.'+changed[0],
                'A repair revision cannot change frozen fields: '+', '.join(changed)+'. Allowed changes: '+(', '.join(mutable) or 'diagnostic context only')+'. Use an authorized replacement for a different Task contract.')
        if user_revision_scope is None and current.type!='agent' and current.model_dump()==replacement.model_dump():raise ValueError('Automatic repair must change the failed implementation')
        if current.type=='script':
            if replacement.parameters!=current.parameters:raise ValueError('Script repair changes argv only')
        elif current.type=='operation':
            if replacement.argv!=current.argv:raise ValueError('Operation repair changes parameters only')
            if user_revision_scope is None and not automatic_revision_retry_safe(current.type,current.operation,current.implementation):
                raise ValueError('An external or non-idempotent operation requires a new authorized task')
        else:
            if replacement.argv!=current.argv or replacement.parameters!=current.parameters:raise ValueError('Agent revision cannot change action permissions')
        specs[identifier]=replacement
    # Redirecting a required dependency is itself an immutable downstream revision.
    for identifier,current in list(specs.items()):
        raw=current.model_dump();changed=False
        for ref in raw['inputs'].values():
            if ref.get('step') in replacements:ref['step']=replacements[ref['step']];changed=True
        if changed:
            updated=Task.model_validate(raw);specs[identifier]=updated;revised[identifier]=updated
    specs.update(added)
    affected=set(revised)|set(replacements)
    while True:
        expanded=affected|{edge['downstream_task_id'] for edge in run['dependencies'] if edge['upstream_task_id'] in affected}
        if expanded==affected:break
        affected=expanded
    for identifier in affected:
        row=existing[identifier]
        if row['lease_id'] and (store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',(row['lease_id'],store.now())) or
                              store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(row['lease_id'],))):
            raise ValueError('Affected Task still has an active lease or cleanup')
        if store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND step=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],identifier)):
            raise ValueError('Affected Task has an unresolved external call')
    calls={}
    submitted=set()
    for call in store.query('SELECT step,revision,implementation FROM workflow_provider_calls WHERE workflow=?',(run['id'],)):
        group=json.loads(call['implementation'])['budget_group'];calls[group]=calls.get(group,0)+1
        submitted.add((call['step'],call['revision']))
    prior_inputs={identifier:Task.model_validate(row['spec']).inputs for identifier,row in existing.items()}
    replacement_sources={new:old for old,new in replacements.items()}
    for task in specs.values():
        task_scope=authorization if task.id in added or task.id in revised else Authorization.model_validate(authority_for(store,run,existing[task.id]))
        if task.on_failure=='diagnose' and 'media_failure_diagnostician' not in task_scope.allowed_agent_profiles:
            raise AuthorizationRequired('Failure diagnosis requires an authorized diagnostic Agent')
        if task.type!='agent':
            from .workflows import Step
            Step.model_validate(task.execution_spec([parent for parent,child in edges if child==task.id]))
        if task.type=='agent':
            if task.profile not in task_scope.allowed_agent_profiles:raise AuthorizationRequired('Agent profile requires additional authorization')
            # Unchanged tasks execute their immutable profile_json under the
            # authorization version frozen when they entered the graph. A
            # later release must not retroactively invalidate completed or
            # unrelated tasks while validating a new proposal.
            if task.id in added or task.id in revised:
                from .specialist_profiles import authorization_snapshot
                if task_scope.agent_profile_digests.get(task.profile)!=digest(authorization_snapshot(task.profile)):
                    raise AuthorizationRequired('Agent policy changed and requires renewed authorization')
            from .agent_runtime_capabilities import validate as validate_runtime
            validate_runtime(task.runtime_requirements.model_dump())
            if task.id in added or task.id in revised:
                from .agent_profiles import for_specialist
                configuration=for_specialist(store,run['id'],task.profile)
                minimum=configuration.specialist_minimum_seconds.get(task.profile,10)
                if task.timeout_seconds<minimum:
                    collection='add_tasks' if task.id in added else 'revise_tasks'
                    source=proposal.add_tasks if task.id in added else proposal.revise_tasks
                    index=next(i for i,item in enumerate(source) if item.id==task.id)
                    raise ProposalFieldError(f'{collection}.{index}.timeout_seconds',
                        f'The frozen {configuration.model} profile requires at least {minimum} seconds for {task.profile}; increase the declared Task timeout before confirmation.')
            if not set(task.runtime_requirements.network_urls)<=set(task_scope.allowed_network_urls):
                raise AuthorizationRequired('Public HTTPS URL requires explicit authorization')
            for name in task.runtime_requirements.gateway_operations:
                if (name not in task_scope.allowed_operations
                        or name not in task_scope.operation_policy_digests
                        or task_scope.provider_call_budget.get(name,0)<=0):
                    raise AuthorizationRequired('Native Gateway operation requires explicit authorization and provider budget')
        else:
            name=task.operation if task.type=='operation' else 'script'
            if name not in task_scope.allowed_operations:raise AuthorizationRequired('Operation requires additional authorization')
            if task.type=='operation' and task_scope.operation_implementations.get(name)!=task.implementation:
                raise AuthorizationRequired('Operation implementation requires renewed authorization')
            if task.type=='operation':
                from .provider_metadata import metadata
                if name not in task_scope.operation_policy_digests:
                    raise AuthorizationRequired('Operation side-effect policy was not frozen')
            if task.type=='operation' and operation(task.operation,task.implementation).external_side_effects:
                group=operation(task.operation,task.implementation).budget_group
                existing_task=existing.get(task.id)
                if task.id in added or task.id in revised or existing_task and existing_task['status']!='succeeded' and (task.id,existing_task['revision']) not in submitted:
                    calls[group]=calls.get(group,0)+1
                if authorization.cost_budget is not None:
                    raise AuthorizationRequired('A finite cost budget requires a verified provider price bound; unavailable')
        for ref in task.inputs.values():
            if ref.repair_candidate:
                from .repair_inputs import lookup
                # An immutable candidate already bound to this logical Task
                # remains readable after its producer is retired. A revision
                # or replacement does not turn that same input into a new grant.
                inherited=prior_inputs.get(replacement_sources.get(task.id,task.id),{})
                if ref in inherited.values():
                    lookup(store,run,ref.repair_candidate.model_dump());continue
                allowed_sources=set(user_revision_scope) if user_revision_scope is not None else set(existing) if repair_scope=='' else {repair_scope} if repair_scope else set()
                if ref.repair_candidate.logical_task_id not in allowed_sources:raise ValueError('Candidate is outside the frozen repair scope')
                lookup(store,run,ref.repair_candidate.model_dump())
            elif ref.asset:
                if ref.asset not in known_assets:
                    raise ValueError('Input asset does not exist in this project')
                if ref.asset not in task_scope.allowed_assets:raise AuthorizationRequired('Input asset is not authorized')
                current_asset=owned_assets[ref.asset]
                if (task.id in added or task.id in revised or existing[task.id]['status']!='succeeded') and task_scope.allowed_asset_digests.get(ref.asset)!=asset_digest(current_asset):
                    raise AuthorizationRequired('Approved input content changed; renew its authorization')
            else:
                if ref.step in replacements:
                    raise ValueError('Input must reference the replacement Task, not a retired Task')
                if (ref.step,task.id) not in edges:raise ValueError('Input must declare its upstream dependency')
                if (task.id in added or task.id in revised) and (task.purpose=='evidence' and ref.step in existing and ref.step not in revised
                        and existing[ref.step]['status']!='succeeded'):
                    raise ValueError('Evidence for a paused or failed Task must use a frozen repair candidate')
                parent=specs[ref.step]
                roles=operation(parent.operation,parent.implementation).outputs if parent.type=='operation' else parent.output_roles
                if ref.role not in roles:raise ValueError('Unknown upstream output role')
        validate_operation_input_constraints(task,specs,owned_assets)
        validate_delivery_dependencies(task,specs)
    if any(count>authorization.provider_call_budget.get(name,0) for name,count in calls.items()):
        raise AuthorizationRequired('Cumulative provider call budget exceeded')
    if not set(proposal.replan_after)<=set(specs):raise ValueError('Unknown replan trigger')
    if proposal.sealed and proposal.replan_after:raise ValueError('Sealed graph cannot request another planning round')
    # apply() gives revised roots and their descendants fresh immutable revisions.
    # Validate the resulting wait graph, not the descendants' old paused states.
    restarted=set(revised)
    while True:
        expanded=restarted|{child for parent,child in edges if parent in restarted}
        if expanded==restarted:break
        restarted=expanded
    unresolved={identifier for identifier,task in existing.items() if task['status'] in ('paused','failed')}-restarted-set(replacements)-set(proposal.review_again)
    if proposal.sealed and unresolved:
        raise ValueError('Sealed repair plan must resolve paused tasks through revisions, replacements or review_again: '+','.join(sorted(unresolved)))
    if not proposal.sealed and not proposal.replan_after:raise ValueError('Open graph needs an explicit replan trigger')
    unreachable={t for t in proposal.replan_after if t in existing and existing[t]['status'] in ('paused','failed')
                 and t not in restarted and t not in replacements and t not in proposal.review_again}
    if unreachable:raise ValueError('Replan trigger cannot wait on an unresolved paused or failed Task: '+','.join(sorted(unreachable)))
    if not proposal.sealed and not any(t in added or t in restarted or existing[t]['status'] not in TERMINAL for t in proposal.replan_after):
        raise ValueError('Replan trigger is already exhausted')
    if not specs:raise ValueError('A workflow must contain at least one business task')
    if proposal.sealed and not any(task.delivery_stage for task in specs.values()):
        raise ValueError('A sealed graph requires an explicit delivery_stage Task')
    return proposal,[added[name] for name in order if name in added],revised,edges


def execution_view(run):
    """Stable execution adapter shape; it confers no mutation authority."""
    steps=[]
    for task in active_tasks(run):
        dependencies=[d['upstream_task_id'] for d in run['dependencies'] if d['downstream_task_id']==task['id']]
        spec=Task.model_validate(task['spec']).execution_spec(dependencies)
        phase_status={'local':'pending','sync':'pending','submit':'pending','poll':'waiting','collect':'collecting','validate':'verifying'}.get(task['phase'],'pending')
        steps.append({'id':task['id'],'workflow':run['id'],'spec':spec,
            'status':'succeeded' if task['status']=='succeeded' else phase_status,
            'revision':task['revision'],'attempts':task['attempts'],'failures':task['attempts'],
            'external_id':task['external_id'],'bindings':task['bindings'],'candidate':task['candidate_result'],
            'result':task['accepted_result'],'quality':task['quality'],'next_poll':task['next_run_at'],
            'started':task['created_at'],'updated':task['updated_at'],'error':task['error_detail'],
            'error_code':task['error_code'],'wait_reason':task['wait_reason'],'repair_count':task.get('repair_count',0),
            'diagnostics':[],'task_status':task['status'],'semantics':task['spec']['semantics'],'purpose':task['spec']['purpose']})
    return {'id':run['id'],'project':run['project_id'],'run_id':run['id'],'name':run['goal'],
            'status':run['status'].lower(),'steps':steps,'dynamic':True,'graph_version':run['graph_version'],'sealed':run['sealed'],
            'agent_review':None,'user_requests':[],'error':run['terminal_reason'],'plan_id':'',
            'created':run['created_at'],'updated':run['updated_at'],
            'definition':{'key':run['id'],'name':run['goal'],'steps':[s['spec'] for s in steps]}}


def initial_phase(task):
    if task.type!='operation':return 'local'
    from .operations import get
    from .operation_lifecycle import for_operation
    return for_operation(get(task.operation,task.implementation)).protocol.state_phases['pending']


def work_items(run):
    return active_tasks(run)+run['control_jobs']


def active_tasks(run):
    return [task for task in run['tasks'] if task['selected']]


def work_item(run,identifier):
    return next((item for item in work_items(run) if item['id']==identifier),None)


def work_table(item):
    return 'control_jobs' if item.get('control_kind') else 'run_tasks'


def write_work(store,item,sql,parameters):
    if not sql.startswith('UPDATE run_tasks '):raise ValueError('Expected work item update')
    return store.execute(sql.replace('UPDATE run_tasks ','UPDATE '+work_table(item)+' ',1),parameters)


def activity(run):
    if run['status']!='ACTIVE':return 'IDLE'
    active=[job for job in run['control_jobs'] if job['status'] in ('ready','running','retry_wait')]
    if any(job['control_kind']=='PLAN' for job in active):return 'PLANNING'
    if any(job['control_kind'] in ('REVIEW','DIAGNOSE') for job in active):return 'REVIEWING'
    if any(task['status'] in ('ready','running','waiting','retry_wait') for task in active_tasks(run)):return 'EXECUTING'
    return 'IDLE'


def authority_for(store,run,item):
    version=item['authorization_version']
    if version==run['authorization_version']:return run['authorization']
    cache=run.setdefault('_authorizations',{})
    if version not in cache:
        rows=store.query('SELECT authorization_json FROM run_authorizations WHERE run_id=? AND version=?',(run['id'],version))
        if not rows:raise ValueError('Frozen task authorization is missing')
        cache[version]=json.loads(rows[0]['authorization_json'])
    return cache[version]
