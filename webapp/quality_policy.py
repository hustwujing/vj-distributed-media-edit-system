"""Versioned, explicit acceptance conditions. Semantic claims require review."""
from typing import Literal
from pathlib import PurePosixPath
from .text_formats import TEXT_SUFFIXES,SUBTITLE_SUFFIXES
from pydantic import BaseModel,ConfigDict,Field,model_validator


OUTPUT_SUFFIXES={'text':TEXT_SUFFIXES,'subtitle':SUBTITLE_SUFFIXES,'json':{'.json'},'image':{'.png','.jpg','.jpeg','.webp'},
    'media':{'.mp4','.mov','.webm','.mkv','.mp3','.wav','.m4a','.flac','.ogg','.aac','.opus'}}
OUTPUT_SUFFIXES['image']=OUTPUT_SUFFIXES['image']|{'.gif'}


def output_target_matches(check,filename):
    kind=check.get('kind','file');suffix=PurePosixPath(filename).suffix.lower()
    if kind=='file':return True
    if suffix not in OUTPUT_SUFFIXES.get(kind,set()):return False
    if kind=='media' and check.get('video') is True:
        return suffix in {'.mp4','.mov','.webm','.mkv'}
    return True


def validate_typed_output_targets(checks,filenames):
    # Use the same file type vocabulary for declaration and delivery binding.
    for check in checks:
        if check.kind not in OUTPUT_SUFFIXES or check.severity=='signal':continue
        selected=[name for name in filenames if check.file in ('*',name)]
        if not any(output_target_matches(check.model_dump(),name) for name in selected):
            raise ValueError(f'{check.kind} 验收没有对应类型的已声明输出；请声明要验收的文件，或将上游检查结果作为报告验收')


class Acceptance(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    kind:Literal['file','image','media','text','review','agent','json','subtitle']='file'
    target: Literal['file','image','media','text','json','subtitle'] | None = Field(
        default=None,description='Output type, not a filename, path or role. Use file to select a filename. '
        'With target present, program reviewer uses that type as kind; agent/user reviewer uses agent/review kind.')
    reviewer: Literal['program','agent','user'] = 'program'
    severity: Literal['required','signal'] = 'required'
    source:Literal['user','technical','product_default','suggestion']='technical'
    stage:Literal['current','final']='current'
    file:str=Field(default='*',pattern=r'^(\*|[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_.\u3400-\u9fff-]{0,99})$')
    criterion:str=Field(default='',max_length=1000)
    width:int|None=Field(default=None,ge=1,le=20000)
    height:int|None=Field(default=None,ge=1,le=20000)
    min_duration:float|None=Field(default=None,ge=0,le=86400)
    max_duration:float|None=Field(default=None,gt=0,le=86400)
    audio_track_present:bool|None=None
    audio_silent:bool|None=Field(default=None,description='True means no audible sound: no audio tracks, or every audio track measures silent. Require audio_track_present=true separately when a silent track must be retained. Loudness measurements use the first audio track.')
    channel_layout:str|None=Field(default=None,max_length=80)
    min_loudness_lufs:float|None=Field(default=None,ge=-120,le=20)
    max_loudness_lufs:float|None=Field(default=None,ge=-120,le=20)
    tone_hz:float|None=Field(default=None,gt=0,le=24000)
    tone_tolerance_hz:float=Field(default=5,gt=0,le=2000)
    video:bool|None=None
    max_bytes:int|None=Field(default=None,ge=1,le=2*1024**3)
    contains:list[str]=Field(default_factory=list,max_length=20)
    text_match:Literal['contains','token_sequence','line_sequence','normalized_text','exact_text']='contains'
    has_alpha:bool|None=None
    transparent_subject:bool|None=Field(default=None,description='Require alpha=0 background pixels and alpha>=128 subject pixels (8-bit alpha). This does not establish semantic subject correctness.')
    min_transparent_fraction:float|None=Field(default=None,ge=0,le=1,description='Minimum fraction of pixels with 8-bit alpha exactly 0.')
    min_visible_fraction:float|None=Field(default=None,ge=0,le=1,description='Minimum fraction of pixels with 8-bit alpha >=128, matching mask topology.')
    max_partial_alpha_fraction:float|None=Field(default=None,ge=0,le=1,description='Maximum fraction with 8-bit alpha from 1 through 254, excluding fully transparent/opaque pixels.')
    max_alpha_components:int|None=Field(default=None,ge=1,le=10000)
    max_alpha_hole_fraction:float|None=Field(default=None,ge=0,le=1)
    fps:float|None=Field(default=None,gt=0,le=240)
    decode:bool=False
    checker: str | None = Field(default=None,pattern=r'^edit/[A-Za-z0-9_./-]+\.py$')
    required_keys:list[str]=Field(default_factory=list,max_length=40)
    @classmethod
    def fields_for_kind(cls,kind):
        common={'kind','severity','source','stage','file'}
        fields={
            'file':{'max_bytes','checker'},
            'image':{'width','height','max_bytes','checker','has_alpha','transparent_subject','min_transparent_fraction',
                     'min_visible_fraction','max_partial_alpha_fraction','max_alpha_components','max_alpha_hole_fraction'},
            'media':{'width','height','min_duration','max_duration','audio_track_present','audio_silent','channel_layout',
                     'min_loudness_lufs','max_loudness_lufs','tone_hz','tone_tolerance_hz','video','max_bytes','fps','decode','checker'},
            'text':{'contains','text_match','max_bytes','checker'},'json':{'required_keys','max_bytes','checker'},
            'subtitle':{'max_bytes','checker'},'review':{'criterion'},'agent':{'criterion'}}
        return common|fields[kind]
    @model_validator(mode='after')
    def valid(self):
        if self.checker and ('..' in self.checker.split('/') or self.reviewer!='program'):raise ValueError('自定义检查器必须是 edit 内程序检查')
        if self.target is not None:
            expected='agent' if self.reviewer=='agent' else 'review' if self.reviewer=='user' else self.target
            if 'kind' in self.model_fields_set and self.kind!=expected:raise ValueError('新旧验收字段冲突')
            self.kind=expected
        elif self.kind in ('agent','review'):
            self.reviewer='agent' if self.kind=='agent' else 'user'
        if self.kind in ('review','agent') and not self.criterion.strip():raise ValueError('内容验收需描述验收标准')
        if self.kind in ('review','agent') and self.severity=='signal':raise ValueError('内容验收是放行条件，signal 仅用于程序检查')
        if self.kind not in ('review','agent') and self.criterion:
            raise ValueError('criterion 只能用于 agent/review；程序验收请填写可计算条件')
        allowed=self.fields_for_kind(self.kind)
        for name in ('width','height','min_duration','max_duration','audio_track_present','audio_silent','channel_layout','min_loudness_lufs','max_loudness_lufs','tone_hz','video','max_bytes','contains'):
            value=getattr(self,name)
            if value is not None and value!=[] and name not in allowed:
                raise ValueError(f'{self.kind} 验收不支持 {name}')
        if self.kind=='text' and not any(x.strip() for x in self.contains):raise ValueError('文本验收必须提供非空 contains')
        if self.kind=='text' and self.text_match in ('exact_text','normalized_text') and len(self.contains)!=1:
            raise ValueError('精确/规范化文本验收只接受一个期望值')
        if any(not text.strip() for text in self.contains):raise ValueError('contains 不得包含空文本')
        if any(len(text)>500 for text in self.contains):raise ValueError('验收文本过长')
        alpha_fields=(self.has_alpha,self.transparent_subject,self.min_transparent_fraction,self.min_visible_fraction,self.max_partial_alpha_fraction,
                      self.max_alpha_components,self.max_alpha_hole_fraction)
        if any(value is not None for value in alpha_fields) and self.kind!='image':raise ValueError('透明度检查需要 image')
        if (self.fps is not None or self.decode) and self.kind!='media':raise ValueError('解码/帧率检查需要 media')
        if self.required_keys and self.kind!='json':raise ValueError('required_keys 仅用于 json')
        if self.min_duration is not None and self.max_duration is not None and self.min_duration>self.max_duration:
            raise ValueError('时长范围无效')
        if self.min_loudness_lufs is not None and self.max_loudness_lufs is not None and self.min_loudness_lufs>self.max_loudness_lufs:
            raise ValueError('响度范围无效')
        if self.source=='suggestion' and self.severity=='required':raise ValueError('建议不能升级为阻断验收')
        if self.source=='user' and self.severity!='required':raise ValueError('用户硬要求不能降级为非阻断信号')
        return self


class Repair(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason:str=Field(min_length=1,max_length=1000)
    argv:list[str]=Field(min_length=1,max_length=100)
    when:list[str]=Field(default_factory=list,max_length=20)
    @model_validator(mode='after')
    def valid(self):
        if any(not arg or len(arg)>8000 or '\x00' in arg for arg in self.argv):raise ValueError('修复命令参数无效')
        allowed={'integrity','missing','width','height','audio_track_present','audio_silent','channel_layout','loudness','tone','video','min_duration','max_duration','max_bytes','contains','review','alpha','fps','decode','schema','subtitle','unsupported_format','encoding'}
        if any(code not in allowed for code in self.when):raise ValueError('未知修复触发条件')
        return self
