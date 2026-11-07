"""Per-operation parameter contracts used by discovery and validation."""
from functools import lru_cache
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,create_model,model_validator
from .models import Segment,Overlay


class Parameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class SegmentOptions(Parameters):
    refine:bool=True
    confidence:float=Field(default=.35,ge=.01,le=.99)


class RefineMaskOptions(Parameters):
    grow:int=Field(default=0,ge=-100,le=100)
    feather:float=Field(default=0,ge=0,le=100)
    threshold:int=Field(default=128,ge=0,le=255)
    remove_small:int=Field(default=0,ge=0,le=1_000_000)
    fill_holes:bool=False


class ProductLayoutOptions(Parameters):
    width:int=Field(ge=16,le=4096)
    height:int=Field(ge=16,le=4096)
    background:str=Field(default='#ffffff',pattern=r'^#[0-9A-Fa-f]{6}$')
    margin:int|None=Field(default=None,ge=0,le=4096)
    shadow:bool=True
    shadow_blur:float=Field(default=18,ge=0,le=100)
    shadow_x:int=Field(default=8,ge=-4096,le=4096)
    shadow_y:int=Field(default=12,ge=-4096,le=4096)


class ColorMatchOptions(Parameters):
    strength:float=Field(default=1,ge=0,le=1)


class LocalAdjustOptions(Parameters):
    brightness:float=Field(default=1,ge=.2,le=3)
    contrast:float=Field(default=1,ge=.2,le=3)
    saturation:float=Field(default=1,ge=0,le=3)
    temperature:float=Field(default=0,ge=-1,le=1)
    gamma:float=Field(default=1,ge=.1,le=5)
    black_point:int=Field(default=0,ge=0,le=254)
    white_point:int=Field(default=255,ge=1,le=255)
    sharpen:float=Field(default=0,ge=0,le=5)
    blur:float=Field(default=0,ge=0,le=50)
    auto_white_balance:bool=False
    auto_levels:bool=False
    @model_validator(mode='after')
    def levels(self):
        if self.black_point>=self.white_point:raise ValueError('Black point must be below white point')
        return self


class BatchExportOptions(Parameters):
    width:int=Field(ge=16,le=4096)
    height:int=Field(ge=16,le=4096)
    fit:Literal['contain','cover']='contain'
    background:str=Field(default='#ffffff',pattern=r'^#[0-9A-Fa-f]{6}$')
    format:Literal['png','jpg','webp']='png'
    quality:int=Field(default=90,ge=40,le=100)
    watermark:str=Field(default='',max_length=100)


IMAGE_OPTION_MODELS={'segment_sam3':SegmentOptions,'refine_mask':RefineMaskOptions,
    'product_layout':ProductLayoutOptions,'color_match':ColorMatchOptions,
    'local_adjust':LocalAdjustOptions,'batch_export':BatchExportOptions}


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
    type:Literal['cut','fade','pixel_dissolve','wipeleft','wiperight','slideleft','slideright','fadeblack']=Field(
        default='cut',description='Transition effect. fade is a linear crossfade; pixel_dissolve randomly replaces pixels and is not a crossfade.')
    duration:float|None=Field(default=None,ge=0,le=3,description='Overlap duration in seconds; overlaps shorten the assembled timeline.')
    @model_validator(mode='after')
    def cut_duration(self):
        if self.duration is None:self.duration=0 if self.type=='cut' else .4
        if self.type=='cut' and self.duration!=0:raise ValueError('Cut transition duration must be zero')
        if self.type!='cut' and self.duration<=0:raise ValueError('Non-cut transition duration must be positive')
        return self


class EditAudio(Parameters):
    asset:str|None=Field(default=None,min_length=1,max_length=100)
    mode:Literal['mix','replace']='mix'
    audio_track:int=Field(default=0,ge=0,le=15)
    start:float|None=Field(default=None,ge=0,description='Start of the selected region in the input audio file, in seconds. This is not its placement on the output timeline.')
    end:float|None=Field(default=None,gt=0,description='End of the selected region in the input audio file, in seconds. Omit to use its actual duration.')
    offset:float=Field(default=0,ge=0,description='Placement time of the selected audio region on the output timeline, in seconds. For music entering late, set this field to the requested entry time.')
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
    clip_to_output:bool=Field(default=True,description='For output-timeline SRT, discard cues after the rendered duration and clip a cue crossing the end. Disable for strict rejection.')
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
    audio_mode:Literal['preserve','remove']='preserve'
    threads:int=Field(default=2,ge=1,le=4)
    @model_validator(mode='after')
    def valid_roles(self):
        import re
        if len(set(self.roles))!=len(self.roles) or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',role) for role in self.roles):
            raise ValueError('Sequence roles must be unique input role names')
        return self


class NormalizeClip(Parameters):
    width:int=Field(ge=16,le=4096,multiple_of=2)
    height:int=Field(ge=16,le=4096,multiple_of=2)
    fps:str=Field(pattern=r'^(24|25|30|50|60|24000/1001|30000/1001|60000/1001)$')
    crf:int=Field(default=20,ge=12,le=35)
    audio_mode:Literal['preserve','remove']='preserve'
    threads:int=Field(default=2,ge=1,le=4)


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


class EvidenceParameters(Parameters):
    mode:Literal['auto','image','video_window','audio','audio_envelope','audio_compare']='auto'
    offset:float=Field(default=0,ge=0,le=86400)
    reference_offset:float|None=Field(default=None,ge=0,le=86400,
        description='audio_compare only: start time in the reference input. Defaults to offset. For a trimmed candidate, compare its own offset with the corresponding original-source time.')
    window_seconds:float=Field(default=8,gt=0,le=8)
    crop:list[int]|None=Field(default=None,min_length=4,max_length=4)
    @model_validator(mode='after')
    def crop_bounds(self):
        if self.crop and not (0<=self.crop[0]<self.crop[2]<=20000 and 0<=self.crop[1]<self.crop[3]<=20000):raise ValueError('Invalid evidence crop')
        if self.reference_offset is not None and self.mode!='audio_compare':raise ValueError('Reference offset requires audio_compare')
        return self


MEDIA_MODELS={'render':Render,'edit':Edit,'analyze':Analyze,'timeline':Timeline,'review':Parameters,'pack':Pack,
              'concat_sequence':ConcatSequence,'normalize_clip':NormalizeClip,'audio_process':AudioProcess,'evidence':EvidenceParameters}


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
    fields={name:(field.annotation,field) for name,field in ImageTask.model_fields.items() if name in IMAGE_PARAMETERS[action]}
    if 'parameters' in fields:
        options=IMAGE_OPTION_MODELS[action]
        fields['parameters']=(options,Field(default_factory=options))
    return create_model(operation_id.replace('.','_')+'Parameters',__base__=Parameters,**fields)
