"""Per-operation parameter contracts used by discovery and validation."""
from functools import lru_cache
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,create_model,model_validator
from .models import Segment,Overlay


class Parameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class Render(Parameters):
    segments:list[Segment]=Field(min_length=1,max_length=100)
    overlays:list[Overlay]=Field(default_factory=list,max_length=8)
    grade:Literal['none','auto','subtle','neutral_punch','warm_cinematic']='none'
    quality:Literal['draft','preview','final']='draft'
    fps:str=Field(default='',pattern=r'^(|24|25|30|50|60|24000/1001|30000/1001|60000/1001)$')
    subtitles:Literal['none','transcript','upload']='none'
    subtitle_asset:str|None=None
    normalize_audio:bool=True
    @model_validator(mode='after')
    def ranges(self):
        if any(segment.end<=segment.start for segment in self.segments):raise ValueError('Segment end must follow start')
        if self.subtitles=='upload' and not self.subtitle_asset:raise ValueError('Uploaded subtitles require an input reference')
        return self


class Edit(Parameters):
    edit_plan:dict=Field(min_length=1)
    @model_validator(mode='after')
    def asset_references(self):
        from .edit_tools import edit_asset_ids
        edit_asset_ids(self.edit_plan)
        return self


class Analyze(Parameters):
    start:float=Field(default=0,ge=0)
    end:float=Field(default=5,gt=0)
    frames:int=Field(default=6,ge=2,le=16)
    @model_validator(mode='after')
    def interval(self):
        if self.end<=self.start:raise ValueError('Analysis end must follow start')
        return self


class Timeline(Analyze):
    audio_track:int=Field(default=0,ge=0,le=15)


class Pack(Parameters):
    silence:float=Field(default=.5,ge=.1,le=5)


MEDIA_MODELS={'render':Render,'edit':Edit,'analyze':Analyze,'timeline':Timeline,'review':Parameters,'pack':Pack}


@lru_cache(maxsize=128)
def model(operation_id,executor,action):
    if executor=='fixed_media':return MEDIA_MODELS[action]
    if executor=='image_sync':
        from .image_generation import Parameters as ImageGeneration
        return ImageGeneration
    from .operations import TranscriptionParameters,GenerationParameters,IMAGE_PARAMETERS
    if executor=='transcribe':return TranscriptionParameters
    if executor=='generation_async':
        if operation_id!='image.generate_from_text':return GenerationParameters
        return create_model('TextImageParameters',__base__=Parameters,**{
            name:(field.annotation,field) for name,field in GenerationParameters.model_fields.items() if name!='duration'})
    from .image_document import ImageTask
    return create_model(operation_id.replace('.','_')+'Parameters',__base__=Parameters,**{
        name:(field.annotation,field) for name,field in ImageTask.model_fields.items() if name in IMAGE_PARAMETERS[action]})
