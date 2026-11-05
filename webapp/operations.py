"""Server-owned operation contracts; no caller-selected executables or URLs."""
from dataclasses import dataclass, asdict, replace,field
import os
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RepairCandidateRef(BaseModel):
    model_config=ConfigDict(extra='forbid')
    logical_task_id:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    revision:int=Field(ge=0)
    artifact_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    role:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')


class InputRef(BaseModel):
    model_config=ConfigDict(extra='forbid')
    asset: str | None = Field(default=None,min_length=1,max_length=100)
    step: str | None = Field(default=None,pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    role: str | None = Field(default=None,pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    repair_candidate:RepairCandidateRef|None=None
    @model_validator(mode='after')
    def reference(self):
        if self.repair_candidate:
            if self.asset or self.step or self.role:raise ValueError('Repair-only input cannot be mixed with an accepted input')
        elif self.asset:
            if self.step or self.role:raise ValueError('素材引用不能混合步骤引用')
        elif not self.step or not self.role:raise ValueError('输入需要 asset 或 step + role')
        return self


class TranscriptionParameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    audio_track:int=Field(default=0,ge=0,le=15)
    language:str=Field(default='',pattern=r'^[a-zA-Z-]{0,12}$')
    speakers:int|None=Field(default=None,ge=1,le=32)


class GenerationParameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prompt:str=Field(min_length=1,max_length=5000)
    width:int=Field(default=1280,ge=64,le=4096)
    height:int=Field(default=720,ge=64,le=4096)
    duration:float=Field(default=15,gt=0,le=120)
    seed:int=Field(default=1,ge=0,le=2147483647)


IMAGE_PARAMETERS={
    'generate':{'prompt','quality'},'decompose':{'prompt','layer_count'},'remove_bg':set(),
    'segment':{'box','points'},'segment_sam3':{'prompt','box','points','parameters'},'refine_zim':set(),'ocr':set(),
    'erase':{'box','prompt','quality'},'local_edit':{'box','prompt','quality'},'replace_text':{'box','text','size','color'},
    'refine_mask':{'parameters'},'perspective':{'points'},'product_layout':{'parameters'},
    'color_match':{'parameters'},'local_adjust':{'parameters'},'batch_export':{'parameters'}}
MEDIA_PARAMETERS={'render':{'segments','overlays','grade','quality','fps','subtitles','subtitle_asset','normalize_audio'},
    'edit':{'edit_plan'},'timeline':{'start','end','frames','audio_track'},'analyze':{'start','end','frames'},
    'review':set(),'pack':{'silence'},'concat_sequence':{'roles','audio_mode','threads'},
    'normalize_clip':{'width','height','fps','crf','audio_mode','threads'},
    'evidence':{'mode','offset','window_seconds','crop'},
    'audio_process':{'role','format','gain_db','target_lufs','true_peak_db','fade_in','fade_out','highpass_hz','denoise','threads'}}


@dataclass(frozen=True)
class Operation:
    id:str
    action:str
    executor:str
    input_kinds:tuple[str,...]=('image',)
    outputs:tuple[str,...]=('primary_image',)
    gpu:bool=False
    retry_safe:bool=True
    cpu:int=2
    memory_mb:int=2048
    timeout:int=900
    external_side_effects:bool=False
    budget_group:str=''
    concurrency_group:str=''
    route_adapter:str=''
    provider_identity:str=''
    cleanup_category:str=''
    model_profile:str='none'
    implementation_id:str=''
    output_files:tuple[str,...]|None=None
    role_filenames:tuple[tuple[str,str],...]=()
    reuse_safe:bool=False
    evidence_only:bool=False
    execution_mode:str='local_bwrap'
    def __post_init__(self):
        if self.execution_mode not in ('local_bwrap','opensandbox_command','host_control'):
            raise ValueError('Unsupported operation execution mode')
        if self.evidence_only and (self.external_side_effects or self.gpu or self.model_profile!='none'):
            raise ValueError('Verified evidence collectors cannot submit external effects or substitute model judgments')
        from .provider_metadata import validate
        validate(self)
        mapped=dict(self.role_filenames)
        if len(mapped)!=len(self.role_filenames) or not set(mapped)<=set(self.outputs):
            raise ValueError('Operation role filenames must bind unique declared output roles')

    def public(self):
        result=asdict(self)
        from .operation_lifecycle import for_operation
        implementation=for_operation(self)
        result.update(contract_version=1,implementation=implementation.id,lifecycle=str(implementation.lifecycle),
                      recovery_by_phase={phase:implementation.recovery(phase) for phase in (*implementation.protocol.phases,'validate')})
        primary=REGISTRY.get(self.id,self)
        supported={primary.implementation_id or primary.executor+'@1',implementation.id,*_IMPLEMENTATION_BINDINGS.get(self.id,{})}
        if self.id=='image.generate_from_text':supported.add('image_sync@1')
        result['implementations']=sorted(supported)
        result['permissions']={'read_declared_inputs':True,'write_attempt_outputs':True,
            'network':'registered_provider' if self.external_side_effects else 'none','external_side_effects':self.external_side_effects,
            'arbitrary_commands':False,'model_profile':self.model_profile}
        filenames=list(self.output_files) if self.output_files is not None else None
        role_filenames=dict(zip(self.outputs,filenames or []));role_filenames.update(self.role_filenames)
        result['output_contract']={'roles':list(self.outputs),'primary':self.outputs[0] if self.outputs else None,
                                   'role_filenames':role_filenames,
                                   'alternatives':['audio','video'] if self.id=='media.edit' else [],'integrity':'server_verified','filenames':filenames}
        parameter_schema=contract(self).parameters.model_json_schema()
        inputs=contract(self)
        result['input_contract']={'required_roles':sorted(inputs.required_inputs),
                                 'allowed_roles':sorted(inputs.allowed_inputs) if inputs.allowed_inputs is not None else None,
                                 'minimum_inputs':inputs.minimum_inputs,'kinds':list(self.input_kinds),
                                 'role_kinds':inputs.role_kinds}
        if self.executor=='image_sync':result['native_size_guaranteed']=False
        if self.id=='video.generate_from_image' and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan' and self.available():
            from .wan_generation import capabilities
            result['constraints']=capabilities()
        elif self.id.startswith('video.generate_') and os.environ.get('VIDEO_USE_GENERATION_BACKEND')=='ark' and self.available():
            from .ark_generation import capabilities
            result['constraints']=capabilities()['operations'][self.id]
        elif self.id=='image.segment_sam3':
            result['constraints']={'selection_modes':['text','points','box'],'selection_modes_exclusive':True,'text_and_spatial_combination':False,
                                   'refinement_optional':True,'spatial_prompt':'empty',
                                   'text_language':'English','text_max_length':120,'translate_user_intent_before_submission':True}
        elif self.id=='media.render':
            result['constraints']={'long_edge_by_quality':{'draft':1280,'preview':1920,'final':1920},
                                   'preserves_input_resolution':False,'custom_geometry_operation':'media.edit'}
        elif self.route_adapter=='image_edit':
            from .image_providers import EDITOR_CONTRACT
            result['constraints']=dict(EDITOR_CONTRACT)
        result.update(available=self.available(),availability='configured_unverified' if self.external_side_effects and self.available() else 'unconfigured' if not self.available() else 'local_registered',input_schema=InputRef.model_json_schema(),
                      parameters_schema=parameter_schema,implementation_version='workflow-operations-v4',
                      recovery='retry_step' if self.retry_safe else 'reuse_saved_result_or_pause_unknown' if self.executor in ('transcribe','image_sync') else 'query_existing_or_pause_unknown',
                      cancellation='terminate_scope' if self.executor in ('fixed_image','fixed_media') else 'abort_request_upstream_unknown' if self.executor in ('transcribe','image_sync') else 'provider_dependent')
        return result
    def available(self):
        if self.id=='video.generate_from_image' and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan':
            return all(os.environ.get(k) for k in ('VIDEO_USE_IMAGE_VIDEO_URL','VIDEO_USE_IMAGE_VIDEO_KEY','VIDEO_USE_IMAGE_VIDEO_MODEL'))
        if self.executor=='image_sync':
            return bool((os.environ.get('VIDEO_USE_IMAGE_GENERATION_URL') or os.environ.get('VIDEO_USE_MODEL_BASE_URL')) and (os.environ.get('VIDEO_USE_IMAGE_GENERATION_KEY') or os.environ.get('VIDEO_USE_MODEL_API_KEY')))
        if self.id in ('image.generate_from_text','video.generate_from_image','video.generate_from_text'):
            if os.environ.get('VIDEO_USE_GENERATION_BACKEND')=='ark':
                return self.id.startswith('video.') and all(os.environ.get(k) for k in ('VIDEO_USE_GENERATION_URL','VIDEO_USE_GENERATION_KEY','VIDEO_USE_GENERATION_MODEL'))
            return bool(os.environ.get('VIDEO_USE_GENERATION_URL'))
        if self.id=='audio.transcribe':return bool(os.environ.get('ELEVENLABS_API_KEY'))
        return True

    def node_enabled(self):
        declared=os.environ.get('VIDEO_USE_NODE_OPERATIONS','*').split(',')
        return '*' in declared or self.id in {s.strip() for s in declared}


_GPU={'remove_bg':'birefnet','segment':'sam2','segment_sam3':'sam3_zim','refine_zim':'sam3_zim'}
_ACTIONS=('generate','decompose','remove_bg','segment','segment_sam3','refine_zim','ocr',
          'erase','local_edit','replace_text','refine_mask','perspective','product_layout','color_match','local_adjust','batch_export')
_ALIASES={'generate':'image.edit','remove_bg':'image.remove_background','local_edit':'image.edit_region'}
# Known fixed adapter filenames are part of the advertised operation contract.
# None means provider/parameter-dependent names, not an empty result.
_FIXED_OUTPUT_FILES={
    'ocr':('ocr.json','ocr-preview.png'),
    'remove_bg':('foreground.png','mask.png'),'segment':('foreground.png','mask.png'),
    'segment_sam3':('foreground.png','mask.png','sam3-zim.json','sam3-preview.jpg','zim-preview.jpg'),
    'refine_zim':('foreground.png','mask.png','sam3-zim.json','zim-preview.jpg'),
    'refine_mask':('mask.png',),'perspective':('image.png',),
    'product_layout':('image.png',),'color_match':('image.png',),'replace_text':('image.png',)}

REGISTRY={}
for action in _ACTIONS:
    external=action in ('generate','decompose','erase','local_edit')
    roles=(('foreground','mask') if action in _GPU else ('mask',) if action=='refine_mask'
           else ('report','preview') if action=='ocr' else ('primary_image','layers','report') if action=='decompose' else ('primary_image',))
    op=Operation(_ALIASES.get(action,'image.'+action),action,
                 'image_async' if action in ('generate','decompose') else 'image_remote' if external else 'fixed_image',
                 outputs=roles,gpu=action in _GPU,retry_safe=not external,
                 memory_mb=8192 if action in _GPU else 2048,
                 external_side_effects=external,
                 budget_group=_ALIASES.get(action,'image.'+action) if external else '',
                 concurrency_group=('decompose' if action=='decompose' else 'edit') if external else '',
                 route_adapter=('image_decompose' if action=='decompose' else 'image_edit') if external else '',
                 provider_identity=('qwen-decompose' if action=='decompose' else 'qwen-image-editor') if external else '',
                 cleanup_category='job_api' if external else '',
                 model_profile=_GPU.get(action,'ocr' if action in ('ocr','replace_text') else 'none'),
                 reuse_safe=action in ('refine_mask','perspective','color_match','local_adjust','batch_export','product_layout'),
                 output_files=_FIXED_OUTPUT_FILES.get(action))
    REGISTRY[op.id]=op
for action in ('render','edit','timeline','analyze','review','pack','concat_sequence','normalize_clip'):
    output_roles={'render':('video','plan','subtitle'),'edit':('video','audio','plan','report'),'timeline':('preview',),'analyze':('report',),'review':('report',),'pack':('text',),'concat_sequence':('video','report'),'normalize_clip':('video','report')}[action]
    input_kinds=('json',) if action=='pack' else ('video','json') if action=='timeline' else ('video',) if action in ('analyze','review','concat_sequence','normalize_clip') else ('video','audio','image','subtitle','json')
    op=Operation('media.'+action,action,'fixed_media',input_kinds,output_roles,memory_mb=4096,timeout=1800,reuse_safe=True,
                 execution_mode='opensandbox_command',
                 output_files=('final.mp4','compiler-report.json') if action in ('concat_sequence','normalize_clip') else None)
    REGISTRY[op.id]=op
REGISTRY['media.evidence']=Operation('media.evidence','evidence','fixed_media',('image','video','audio'),('report','preview'),
    cpu=1,memory_mb=2048,timeout=300,reuse_safe=True,evidence_only=True,output_files=('evidence-report.json','preview.jpg'),execution_mode='opensandbox_command')
REGISTRY['audio.process']=Operation('audio.process','audio_process','fixed_media',('audio','video'),('audio','report'),
    memory_mb=2048,timeout=1800,reuse_safe=True,execution_mode='opensandbox_command',
    role_filenames=(('report','audio-process-report.json'),))
REGISTRY['audio.transcribe']=Operation('audio.transcribe','transcribe','transcribe',('audio','video'),('transcript',),retry_safe=False,external_side_effects=True,budget_group='audio.transcribe',concurrency_group='elevenlabs',route_adapter='elevenlabs',provider_identity='elevenlabs',cleanup_category='unqueryable_sync',output_files=('transcript.json',))
REGISTRY['image.generate_from_text']=Operation('image.generate_from_text','text_to_image','generation_async',(),('primary_image',),retry_safe=False,external_side_effects=True,budget_group='image.generate_from_text',concurrency_group='generation',route_adapter='generation_jobs',provider_identity='generation-jobs',cleanup_category='job_api')
REGISTRY['video.generate_from_image']=Operation('video.generate_from_image','image_to_video','generation_async',('image',),('video',),retry_safe=False,external_side_effects=True,budget_group='video.generate_from_image',concurrency_group='generation',route_adapter='generation_jobs',provider_identity='generation-jobs',cleanup_category='job_api',timeout=3600)
REGISTRY['video.generate_from_text']=Operation('video.generate_from_text','text_to_video','generation_async',(),('video',),retry_safe=False,external_side_effects=True,budget_group='video.generate_from_text',concurrency_group='generation',route_adapter='generation_jobs',provider_identity='generation-jobs',cleanup_category='job_api',timeout=3600)


@dataclass(frozen=True)
class OperationContract:
    parameters:type[BaseModel]
    required_inputs:frozenset[str]=frozenset()
    allowed_inputs:frozenset[str]|None=None
    minimum_inputs:int=0
    role_kinds:dict[str,tuple[str,...]]=field(default_factory=dict)

    def validate(self,parameters,inputs):
        if not self.required_inputs.issubset(inputs) or len(inputs)<self.minimum_inputs:
            raise ValueError('Operation is missing required inputs')
        if self.allowed_inputs is not None and set(inputs)-self.allowed_inputs:
            raise ValueError('Operation received undeclared input roles')
        for value in inputs.values():InputRef.model_validate(value)
        return self.parameters.model_validate(parameters).model_dump(exclude_none=True)


_CONTRACTS={}
_IMPLEMENTATION_BINDINGS={}
_VARIANT_CONTRACTS={}


def register_implementation(operation_id,implementation,**resources):
    """Trusted plugin hook: alternatives retain one input/output business contract."""
    from .operation_lifecycle import get as implementation_by_id,for_operation
    if operation_id not in REGISTRY:raise ValueError('Register operation before implementations')
    selected=implementation_by_id(implementation)
    allowed={'cpu','memory_mb','gpu','timeout','external_side_effects','budget_group','concurrency_group','route_adapter','provider_identity','cleanup_category','model_profile','retry_safe','reuse_safe'}
    if set(resources)-allowed:raise ValueError('Implementation cannot override the business contract')
    variants=_IMPLEMENTATION_BINDINGS.setdefault(operation_id,{})
    if implementation in variants or implementation==for_operation(REGISTRY[operation_id]).id:raise ValueError('Duplicate operation implementation')
    definition=contract(REGISTRY[operation_id])
    resources.setdefault('reuse_safe',False)
    variants[implementation]=replace(REGISTRY[operation_id],executor=selected.id.rsplit('@',1)[0],implementation_id=implementation,**resources)
    _VARIANT_CONTRACTS[(operation_id,implementation)]=definition


def register(operation,definition):
    """Trusted service registration; never called from an Agent payload."""
    from .operation_lifecycle import for_operation
    if operation.id in REGISTRY:raise ValueError('Duplicate operation: '+operation.id)
    for_operation(operation)
    if not issubclass(definition.parameters,BaseModel):raise ValueError('Parameter model required')
    if definition.parameters.model_config.get('extra')!='forbid':raise ValueError('Unknown parameters must be rejected')
    if len(set(operation.outputs))!=len(operation.outputs):raise ValueError('Duplicate output roles')
    if not operation.outputs:raise ValueError('Output contract required')
    REGISTRY[operation.id]=operation
    _CONTRACTS[operation.id]=definition


def contract(operation):
    alternative=_VARIANT_CONTRACTS.get((operation.id,operation.implementation_id))
    if alternative:return alternative
    if operation.id in _CONTRACTS:return _CONTRACTS[operation.id]
    from .operation_parameters import model
    parameters=model(operation.id,operation.executor,operation.action)
    if operation.evidence_only:return OperationContract(parameters,frozenset({'source'}),frozenset({'source','reference'}),role_kinds={'reference':('image',)})
    if operation.action=='normalize_clip':return OperationContract(parameters,frozenset({'source'}),frozenset({'source'}))
    if operation.executor=='fixed_media':
        return OperationContract(parameters,minimum_inputs=1)
    if operation.executor in ('generation_async','image_sync'):
        roles=frozenset({'source'}) if operation.input_kinds else frozenset()
        return OperationContract(parameters,roles,roles)
    if operation.executor=='transcribe':return OperationContract(parameters,frozenset({'source'}),frozenset({'source'}))
    return OperationContract(parameters,frozenset({'source'}),frozenset({'source','mask','reference','reference2'}))


def get(operation,implementation=None):
    if operation not in REGISTRY:raise ValueError('未登记的操作: '+str(operation))
    op=REGISTRY[operation]
    if implementation is not None:
        from .operation_lifecycle import get as get_implementation
        selected=get_implementation(implementation)
        if implementation in _IMPLEMENTATION_BINDINGS.get(operation,{}):return _IMPLEMENTATION_BINDINGS[operation][implementation]
        allowed={op.implementation_id or op.executor+'@1'}
        if operation=='image.generate_from_text':allowed.add('image_sync@1')
        if selected.id not in allowed:raise ValueError('Implementation does not implement this operation')
        executor=selected.id.rsplit('@',1)[0]
        return resolved_provider(replace(op,executor=executor,implementation_id=selected.id))
    if operation=='image.generate_from_text' and os.environ.get('VIDEO_USE_IMAGE_GENERATION_BACKEND','auto')!='jobs' and (os.environ.get('VIDEO_USE_IMAGE_GENERATION_URL') or os.environ.get('VIDEO_USE_MODEL_BASE_URL')):
        return resolved_provider(replace(op,executor='image_sync'))
    return resolved_provider(op)


def resolved_provider(op):
    if op.executor=='image_sync':
        return replace(op,route_adapter='openai_images',provider_identity='openai-images',concurrency_group='openai_images',cleanup_category='unqueryable_sync')
    if op.route_adapter in ('generation','generation_jobs'):
        identity=('wan' if op.id=='video.generate_from_image' and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan'
                  else 'ark' if os.environ.get('VIDEO_USE_GENERATION_BACKEND')=='ark' else 'generation-jobs')
        return replace(op,provider_identity=identity,route_adapter='generation_jobs' if identity=='generation-jobs' else 'generation',cleanup_category='job_api' if identity=='generation-jobs' else 'generation_protocol')
    return op


def for_image(action):
    return get(_ALIASES.get(action,'image.'+action))


def for_step(spec):
    if spec['kind']=='operation':return get(spec['operation'],spec.get('implementation'))
    if spec['kind'] in ('script','agent'):return None
    raise ValueError('工作流步骤只支持 script 或 operation')


def validate_parameters(operation,parameters,inputs,implementation=None):
    op=get(operation,implementation)
    normalized=contract(op).validate(parameters,inputs)
    if operation in _CONTRACTS:return normalized
    if (operation,op.implementation_id) in _VARIANT_CONTRACTS:
        # Built-in business validators belong to the operation, not to a newly
        # named backend. Preserve both its schema and action-specific checks.
        from .operation_lifecycle import for_operation
        return validate_parameters(operation,parameters,inputs,for_operation(REGISTRY[operation]).id)
    if op.executor=='image_sync':
        from .image_generation import Parameters
        if inputs:raise ValueError('纯文生图不接受目标图片输入')
        return Parameters.model_validate(parameters).model_dump()
    if op.executor=='fixed_media':
        from .operation_parameters import model
        if op.action!='pack' and not inputs:raise ValueError('媒体操作需要声明输入素材')
        value=model(op.id,op.executor,op.action).model_validate(parameters).model_dump(exclude_none=True)
        if op.action=='render' and not value['segments']:raise ValueError('渲染操作需要 segments')
        if op.action=='edit' and not value['edit_plan']:raise ValueError('编辑操作需要 edit_plan')
        if op.action=='concat_sequence' and set(value['roles'])!=set(inputs):
            raise ValueError('顺序拼接 roles 必须逐一且仅引用已声明输入角色')
        if op.action=='audio_process' and set(inputs)!={value['role']}:
            raise ValueError('音频处理 role 必须是唯一声明输入角色')
        return value
    if op.executor=='transcribe':
        if set(inputs)!={'source'}:raise ValueError('转写需要唯一 source 输入')
        return TranscriptionParameters.model_validate(parameters).model_dump()
    if op.executor=='generation_async':
        value=GenerationParameters.model_validate(parameters).model_dump()
        if not value['prompt'].strip():raise ValueError('生成描述不能为空')
        if op.id=='image.generate_from_text':
            if 'duration' in parameters:raise ValueError('图片生成没有时长参数')
            value.pop('duration',None)
        if op.input_kinds and 'source' not in inputs:raise ValueError('图生视频需要 source 输入')
        if op.input_kinds and set(inputs)!={'source'}:raise ValueError('图生视频只接受 source 输入')
        if not op.input_kinds and inputs:raise ValueError('纯文本生成不接受图片输入')
        return value
    from .image_document import ImageTask
    if set(parameters)-IMAGE_PARAMETERS[op.action]:raise ValueError('图片操作包含不适用的参数，不能覆盖输入或动作')
    if 'source' not in inputs:raise ValueError('图片操作需要 source 输入')
    if set(inputs)-{'source','mask','reference','reference2'}:raise ValueError('未知图片输入角色')
    value=ImageTask.model_validate({'action':op.action,**parameters}).model_dump()
    if op.action=='segment_sam3':
        from .image_document import validate_sam3_prompt
        validate_sam3_prompt(value['prompt'])
    return {k:v for k,v in value.items() if k in IMAGE_PARAMETERS[op.action]}


def catalog():
    from .text_formats import TEXT_SUFFIXES,SUBTITLE_SUFFIXES
    return {'version':4,'acceptance_formats':{'text':sorted(TEXT_SUFFIXES),'subtitle':sorted(SUBTITLE_SUFFIXES),'encoding':'UTF-8 / UTF-8 BOM','max_text_bytes':2*1024**2},
            'intent_contract':{'script_marker':'script',
                'script_meaning':'Running helpers or custom local Pillow/FFmpeg/OpenCV commands, regardless of media type',
                'single_script_example':{'route':'single','operations':['script'],'goal':'Rotate an image with a local helper','estimated_seconds':30},
                'operation_rule':'Use a registered ID only for that controlled operation; image.edit is external generative editing, not generic local image work'},
            'operations':[get(name).public() for name in REGISTRY],
            'step_kinds':['script','operation'],
            'dispatch_kinds':['agent','manual','workflow'],'conversation_modes':['plan','execute']}


from .operation_extensions import load as _load_extensions
_load_extensions()
