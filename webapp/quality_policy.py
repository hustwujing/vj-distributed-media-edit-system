"""Versioned, explicit acceptance conditions. Semantic claims require review."""
from typing import Literal
from pathlib import PurePosixPath
from pydantic import BaseModel,ConfigDict,Field,model_validator


def validate_typed_output_targets(checks,filenames):
    # These are the types recognized by the fixed quality inspector. A script
    # report about an input video does not make that report a media output.
    suffixes={'image':{'.png','.jpg','.jpeg','.webp'},
              'media':{'.mp4','.mov','.webm','.mkv','.mp3','.wav','.m4a','.flac','.ogg','.aac','.opus'}}
    for check in checks:
        if check.kind not in suffixes or check.severity=='signal':continue
        selected=[name for name in filenames if check.file in ('*',name)]
        if not any(PurePosixPath(name).suffix.lower() in suffixes[check.kind] for name in selected):
            raise ValueError(f'{check.kind} 验收没有对应类型的已声明输出；请声明要验收的文件，或将上游检查结果作为报告验收')


class Acceptance(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    kind:Literal['file','image','media','text','review','agent','json','subtitle']='file'
    target: str | None = None
    reviewer: Literal['program','agent','user'] = 'program'
    severity: Literal['required','signal'] = 'required'
    file:str=Field(default='*',pattern=r'^(\*|[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_.\u3400-\u9fff-]{0,99})$')
    criterion:str=Field(default='',max_length=1000)
    width:int|None=Field(default=None,ge=1,le=20000)
    height:int|None=Field(default=None,ge=1,le=20000)
    min_duration:float|None=Field(default=None,ge=0,le=86400)
    max_duration:float|None=Field(default=None,gt=0,le=86400)
    audio:bool|None=None
    video:bool|None=None
    max_bytes:int|None=Field(default=None,ge=1,le=2*1024**3)
    contains:list[str]=Field(default_factory=list,max_length=20)
    has_alpha:bool|None=None
    min_transparent_fraction:float|None=Field(default=None,ge=0,le=1)
    fps:float|None=Field(default=None,gt=0,le=240)
    decode:bool=False
    checker: str | None = Field(default=None,pattern=r'^edit/[A-Za-z0-9_./-]+\.py$')
    required_keys:list[str]=Field(default_factory=list,max_length=40)
    @model_validator(mode='after')
    def valid(self):
        if self.checker and ('..' in self.checker.split('/') or self.reviewer!='program'):raise ValueError('自定义检查器必须是 edit 内程序检查')
        if self.target is not None:
            if self.target not in ('file','image','media','text','json','subtitle'):raise ValueError('未知验收对象')
            expected='agent' if self.reviewer=='agent' else 'review' if self.reviewer=='user' else self.target
            if 'kind' in self.model_fields_set and self.kind!=expected:raise ValueError('新旧验收字段冲突')
            self.kind=expected
        elif self.kind in ('agent','review'):
            self.reviewer='agent' if self.kind=='agent' else 'user'
        if self.kind in ('review','agent') and not self.criterion.strip():raise ValueError('内容验收需描述验收标准')
        if self.kind in ('review','agent') and self.severity=='signal':raise ValueError('内容验收是放行条件，signal 仅用于程序检查')
        if self.kind not in ('review','agent') and self.criterion:
            raise ValueError('criterion 只能用于 agent/review；程序验收请填写可计算条件')
        allowed={'file':{'max_bytes'},'image':{'width','height','max_bytes'},
                 'media':{'width','height','min_duration','max_duration','audio','video','max_bytes'},
                 'text':{'contains','max_bytes'},'json':{'max_bytes'},'subtitle':{'max_bytes'},'review':set(),'agent':set()}[self.kind]
        for name in ('width','height','min_duration','max_duration','audio','video','max_bytes','contains'):
            value=getattr(self,name)
            if value is not None and value!=[] and name not in allowed:
                raise ValueError(f'{self.kind} 验收不支持 {name}')
        if self.kind=='text' and not any(x.strip() for x in self.contains):raise ValueError('文本验收必须提供非空 contains')
        if any(not text.strip() for text in self.contains):raise ValueError('contains 不得包含空文本')
        if any(len(text)>500 for text in self.contains):raise ValueError('验收文本过长')
        if (self.has_alpha is not None or self.min_transparent_fraction is not None) and self.kind!='image':raise ValueError('透明度检查需要 image')
        if (self.fps is not None or self.decode) and self.kind!='media':raise ValueError('解码/帧率检查需要 media')
        if self.required_keys and self.kind!='json':raise ValueError('required_keys 仅用于 json')
        if self.min_duration is not None and self.max_duration is not None and self.min_duration>self.max_duration:
            raise ValueError('时长范围无效')
        return self


class Repair(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason:str=Field(min_length=1,max_length=1000)
    argv:list[str]=Field(min_length=1,max_length=100)
    when:list[str]=Field(default_factory=list,max_length=20)
    @model_validator(mode='after')
    def valid(self):
        if any(not arg or len(arg)>8000 or '\x00' in arg for arg in self.argv):raise ValueError('修复命令参数无效')
        allowed={'integrity','missing','width','height','audio','video','min_duration','max_duration','max_bytes','contains','review','alpha','fps','decode','schema','subtitle'}
        if any(code not in allowed for code in self.when):raise ValueError('未知修复触发条件')
        return self
