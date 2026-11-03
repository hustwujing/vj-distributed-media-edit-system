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
    quality:Literal['draft','preview','final']=Field(default='draft',description='Resolution preset: draft scales the long edge to 1280; preview/final to 1920. Does NOT preserve input size. Use media.edit with explicit output geometry for other dimensions.')
    fps:str=Field(default='',pattern=r'^(|24|25|30|50|60|24000/1001|30000/1001|60000/1001)$')
    subtitles:Literal['none','transcript','upload']='none'
    subtitle_asset:str|None=None
    normalize_audio:bool=True
    @model_validator(mode='after')
    def ranges(self):
        if any(segment.end<=segment.start for segment in self.segments):raise ValueError('Segment end must follow start')
        if self.subtitles=='upload' and not self.subtitle_asset:raise ValueError('Uploaded subtitles require an input reference')
        return self


class EditClip(Parameters):
    asset:str=Field(min_length=1,max_length=100)
    start:float|None=Field(default=None,ge=0)
    end:float|None=Field(default=None,gt=0)
    words:list[int]|None=Field(default=None,min_length=2,max_length=2)
    padding:float=Field(default=.08,ge=.03,le=.2)
    audio_track:int=Field(default=0,ge=0,le=15)
    speed:float=Field(default=1,ge=.25,le=4)
    reverse:bool=False
    reverse_audio:bool=False
    freeze_end:float=Field(default=0,ge=0,le=30)
    repeat:int=Field(default=1,ge=1,le=20)
    crop:list[int]|None=Field(default=None,min_length=4,max_length=4)
    rotate:Literal[0,90,180,270]=0
    flip:Literal['none','horizontal','vertical']='none'
    fit:Literal['contain','cover','blur']='contain'
    mute:bool=False
    volume:float=Field(default=1,ge=0,le=4)
    grade:Literal['none','subtle','neutral_punch','warm_cinematic']='none'
    @model_validator(mode='after')
    def interval(self):
        if self.start is not None and self.end is not None and self.end<=self.start:raise ValueError('Clip end must follow start')
        if self.words is not None and (self.start is not None or self.end is not None):raise ValueError('words replaces clip start/end')
        if self.reverse_audio and not self.reverse:raise ValueError('reverse_audio requires reverse')
        return self


class EditOutput(Parameters):
    width:int|None=Field(default=None,ge=16,le=4096,multiple_of=2)
    height:int|None=Field(default=None,ge=16,le=4096,multiple_of=2)
    fps:int=Field(default=30,ge=1,le=60)
    crf:int=Field(default=20,ge=12,le=35)
    audio_only:bool=False
    format:Literal['mp4','wav','mp3','m4a']|None=None


class EditTransition(Parameters):
    type:Literal['cut','fade','dissolve','wipeleft','wiperight','slideleft','slideright','fadeblack']='cut'
    duration:float=Field(default=0,ge=0,le=3)
    @model_validator(mode='after')
    def cut_duration(self):
        if self.type=='cut' and self.duration!=0:raise ValueError('Cut transition duration must be zero')
        return self


class EditAudio(Parameters):
    asset:str|None=Field(default=None,min_length=1,max_length=100)
    mode:Literal['mix','replace']='mix'
    audio_track:int=Field(default=0,ge=0,le=15)
    start:float|None=Field(default=None,ge=0)
    end:float|None=Field(default=None,gt=0)
    offset:float=Field(default=0,ge=0)
    volume:float=Field(default=1,ge=0,le=4)
    duck:bool=False
    loop:bool=False
    denoise:bool=False
    normalize:bool=False
    highpass:float=Field(default=0,ge=0,le=300)
    fade_in:float=Field(default=.03,ge=.03,le=30)
    fade_out:float=Field(default=.03,ge=.03,le=30)


class EditMask(Parameters):
    x:int=Field(ge=0,le=4096,multiple_of=2)
    y:int=Field(ge=0,le=4096,multiple_of=2)
    width:int=Field(ge=4,le=4096,multiple_of=2)
    height:int=Field(ge=4,le=4096,multiple_of=2)
    start:float=Field(default=0,ge=0)
    end:float|None=Field(default=None,gt=0)
    mode:Literal['pixelate','blur','black']='pixelate'


class EditTitle(Parameters):
    text:str=Field(min_length=1,max_length=500)
    size:int|None=Field(default=None,ge=10,le=200)
    color:str=Field(default='#FFFFFF',pattern=r'^#[0-9A-Fa-f]{6}$')
    background:bool=Field(default=True,description='Whether to draw the title background panel. This is a boolean, not a color.')
    position:Literal['top','center','bottom']='bottom'
    start:float=Field(default=0,ge=0)
    end:float|None=Field(default=None,gt=0)


class EditSubtitles(Parameters):
    asset:str|None=Field(default=None,min_length=1,max_length=100)
    from_transcripts:bool=False
    words_per_line:int=Field(default=6,ge=1,le=20)
    size:int|None=Field(default=None,ge=10,le=200)
    color:str=Field(default='#FFFFFF',pattern=r'^#[0-9A-Fa-f]{6}$')
    highlight:str|None=Field(default=None,pattern=r'^#[0-9A-Fa-f]{6}$')
    position:Literal['top','center','bottom']='bottom'
    outline:int=Field(default=2,ge=0,le=8)


class EditPlan(Parameters):
    version:Literal[1]|None=None
    clips:list[EditClip]=Field(min_length=1,max_length=100)
    output:EditOutput=Field(default_factory=EditOutput)
    transition:EditTransition=Field(default_factory=EditTransition)
    layout:Literal['sequence','horizontal','vertical','grid']='sequence'
    split_audio:int=Field(default=0,ge=0,le=99)
    audio:EditAudio=Field(default_factory=EditAudio)
    masks:list[EditMask]=Field(default_factory=list,max_length=30)
    titles:list[EditTitle]=Field(default_factory=list,max_length=30)
    subtitles:EditSubtitles|None=None


class Edit(Parameters):
    edit_plan:EditPlan
    @model_validator(mode='after')
    def asset_references(self):
        from .edit_tools import edit_asset_ids
        edit_asset_ids(self.edit_plan.model_dump(exclude_none=True))
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


class ConcatSequence(Parameters):
    roles:list[str]=Field(min_length=1,max_length=100)
    width:int|None=Field(default=None,ge=16,le=4096,multiple_of=2)
    height:int|None=Field(default=None,ge=16,le=4096,multiple_of=2)
    fps:str|None=Field(default=None,pattern=r'^(24|25|30|50|60|24000/1001|30000/1001|60000/1001)$')
    crf:int=Field(default=20,ge=12,le=35)
    audio_mode:Literal['preserve','remove']='preserve'
    threads:int=Field(default=2,ge=1,le=4)
    @model_validator(mode='after')
    def valid_roles(self):
        import re
        if len(set(self.roles))!=len(self.roles) or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',role) for role in self.roles):
            raise ValueError('Sequence roles must be unique input role names')
        if (self.width is None)!=(self.height is None):raise ValueError('Output width and height must be set together')
        return self


class AudioProcess(Parameters):
    role:str=Field(default='source',pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    format:Literal['wav','mp3','m4a']='wav'
    gain_db:float=Field(default=0,ge=-60,le=24)
    target_lufs:float|None=Field(default=None,ge=-40,le=-5)
    true_peak_db:float=Field(default=-1.5,ge=-9,le=0)
    fade_in:float=Field(default=0,ge=0,le=30)
    fade_out:float=Field(default=0,ge=0,le=30)
    highpass_hz:float=Field(default=0,ge=0,le=300)
    denoise:bool=False
    threads:int=Field(default=2,ge=1,le=4)


MEDIA_MODELS={'render':Render,'edit':Edit,'analyze':Analyze,'timeline':Timeline,'review':Parameters,'pack':Pack,
              'concat_sequence':ConcatSequence,'audio_process':AudioProcess}


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
