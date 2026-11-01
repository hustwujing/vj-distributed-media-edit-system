"""Bind browser editing options to owned asset paths before using the local helper."""
import copy
import sys
from pathlib import Path

HELPERS=Path(__file__).resolve().parents[1]/'helpers'
if str(HELPERS) not in sys.path:sys.path.insert(0,str(HELPERS))
from edit_plan import validate, PlanError
from edit_captions import cues_for_plan


def edit_asset_ids(raw):
    if not isinstance(raw,dict):raise PlanError('请选择编辑工具并填写参数。')
    if any(k in raw for k in ('sources','transcripts')):raise PlanError('编辑参数只能引用素材 ID。')
    clips=raw.get('clips',[])
    if not isinstance(clips,list) or len(clips)>100:raise PlanError('片段列表无效。')
    refs=set()
    for obj in [*clips,raw.get('audio',{}),raw.get('subtitles',{})]:
        if not isinstance(obj,dict):raise PlanError('编辑参数格式无效。')
        if any(k in obj for k in ('source','file')):raise PlanError('不能在编辑参数中指定文件路径。')
        asset=obj.get('asset')
        if asset:
            if not isinstance(asset,str):raise PlanError('素材 ID 无效。')
            refs.add(asset)
    return refs


def bind_plan(raw, assets, project):
    """Never use caller-supplied filesystem paths, even for subtitle files."""
    refs=edit_asset_ids(raw)
    if not refs.issubset(assets):raise PlanError('请选择当前项目中的素材。')
    p=copy.deepcopy(raw);p['version']=1;p['sources']={};p['transcripts']={}
    for c in p.get('clips',[]):
        asset_id=c.pop('asset',None)
        if asset_id not in assets:raise PlanError('请为每个片段选择素材。')
        asset=assets[asset_id];track=c.get('audio_track',0)
        if asset['metadata']['kind'] not in ('video','audio'):raise PlanError('片段需要音视频素材。')
        if isinstance(track,bool) or not isinstance(track,int) or not 0<=track<=15:raise PlanError('音轨序号无效。')
        alias=f'{asset_id}_t{track}'
        c['source']=alias;p['sources'][alias]='sources/'+asset['filename']
        suffix=f'.track{track}' if track else ''
        transcript=Path(project)/'edit'/'transcripts'/f'{asset_id}{suffix}.json'
        if transcript.is_file():p['transcripts'][alias]=str(transcript)
    audio=p.get('audio',{})
    if audio.get('asset'):
        asset_id=audio.pop('asset');alias=f'audio_{asset_id}'
        p['sources'][alias]='sources/'+assets[asset_id]['filename'];audio['source']=alias
    elif 'asset' in audio:audio.pop('asset')
    subtitles=p.get('subtitles',{})
    if subtitles.get('asset'):
        asset=assets[subtitles.pop('asset')]
        if asset['metadata']['kind']!='subtitle':raise PlanError('请选择本项目上传的 SRT 字幕。')
        subtitles['file']='sources/'+asset['filename']
    elif 'asset' in subtitles:subtitles.pop('asset')
    normalized=validate(p,project)
    if 'subtitles' in normalized:cues_for_plan(normalized)
    return p,normalized
