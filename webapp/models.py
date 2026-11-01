from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ProjectInput(Model):
    name: str = Field(min_length=1, max_length=100)


class BatchDeleteInput(Model):
    ids: list[str] = Field(min_length=1, max_length=100)


class Segment(Model):
    asset: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    audio_track: int = Field(default=0, ge=0, le=15)


class Overlay(Model):
    asset: str
    start: float = Field(ge=0)
    duration: float = Field(gt=0)


class JobInput(Model):
    kind: Literal['transcribe', 'pack', 'timeline', 'analyze', 'review', 'render', 'edit']
    tool: str | None = Field(default=None, pattern=r'^[a-z_]{1,32}$')
    edit_plan: dict | None = None
    assets: list[str] = Field(default_factory=list, max_length=50)
    language: str = Field(default='', pattern=r'^[a-zA-Z-]{0,12}$')
    speakers: int | None = Field(default=None, ge=1, le=32)
    audio_track: int = Field(default=0, ge=0, le=15)
    workers: int = Field(default=2, ge=1, le=4)
    start: float = Field(default=0, ge=0)
    end: float = Field(default=5, gt=0)
    frames: int = Field(default=6, ge=2, le=16)
    silence: float = Field(default=0.5, ge=0.1, le=5)
    segments: list[Segment] = Field(default_factory=list, max_length=100)
    overlays: list[Overlay] = Field(default_factory=list, max_length=8)
    grade: Literal['none', 'auto', 'subtle', 'neutral_punch', 'warm_cinematic'] = 'none'
    quality: Literal['draft', 'preview', 'final'] = 'draft'
    fps: str = Field(default='', pattern=r'^(|24|25|30|50|60|24000/1001|30000/1001|60000/1001)$')
    subtitles: Literal['none', 'transcript', 'upload'] = 'none'
    subtitle_asset: str | None = None
    normalize_audio: bool = True


class DraftInput(Model):
    preset: JobInput | None = None
    prompt: str = Field(default='', max_length=8000)
    editor: Literal['tools','chat','images'] = 'chat'
