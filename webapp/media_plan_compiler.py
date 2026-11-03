"""Compile bounded sequential media plans without unbounded multi-input graphs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


class MediaCompileError(RuntimeError):
    def __init__(self,code,message,stderr=''):
        self.diagnostic={'code':code,'stderr_tail':stderr[-8000:]}
        super().__init__(message)


def probe(path):
    run=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],
                       capture_output=True,text=True,timeout=30)
    if run.returncode:raise MediaCompileError('media_probe_failed','Input media could not be inspected',run.stderr)
    record=json.loads(run.stdout);streams=record.get('streams',[])
    video=next((item for item in streams if item.get('codec_type')=='video' and not item.get('disposition',{}).get('attached_pic')),None)
    audio=next((item for item in streams if item.get('codec_type')=='audio'),None)
    if not video:raise MediaCompileError('media_video_missing','Sequence input has no video stream')
    try:duration=float(record.get('format',{}).get('duration') or video.get('duration') or 0)
    except (TypeError,ValueError):duration=0
    if duration<=0:raise MediaCompileError('media_duration_invalid','Sequence input duration is invalid')
    signature={'video':{key:video.get(key) for key in ('codec_name','width','height','pix_fmt','r_frame_rate','time_base','sample_aspect_ratio')},
               'audio':None if not audio else {key:audio.get(key) for key in ('codec_name','sample_rate','channels','channel_layout','time_base')}}
    return {'duration':duration,'signature':signature,'bytes':Path(path).stat().st_size}


def resource_plan(records,threads,temporary_multiplier=3.0):
    inputs=len(records);total_bytes=sum(item['bytes'] for item in records)
    return {'input_count':inputs,'total_duration':sum(item['duration'] for item in records),
            'estimated_peak_tasks':max(12,threads*4+8),'estimated_temporary_bytes':int(total_bytes*temporary_multiplier),
            'strategy':'concat_demuxer' if len({json.dumps(item['signature'],sort_keys=True) for item in records})==1 else 'normalize_then_concat'}


def _run(command,code):
    run=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if run.returncode:raise MediaCompileError(code,'Bounded media compilation failed',run.stderr)


def _concat_file(paths,target):
    lines=[]
    for path in paths:
        escaped=str(Path(path).resolve()).replace("'","'\\''")
        lines.append("file '"+escaped+"'")
    target.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def compile_sequence(paths,output,workdir,*,width=None,height=None,fps=None,crf=20,audio_mode='preserve',threads=2):
    if not 1<=len(paths)<=100:raise ValueError('Sequence requires 1..100 inputs')
    if not 1<=threads<=4:raise ValueError('Invalid media thread limit')
    paths=[Path(path) for path in paths];output=Path(output);workdir=Path(workdir);workdir.mkdir(parents=True,exist_ok=True)
    records=[probe(path) for path in paths];resources=resource_plan(records,threads)
    available=shutil.disk_usage(workdir).free
    if resources['estimated_temporary_bytes']>available:
        raise MediaCompileError('media_temporary_space_insufficient','Insufficient temporary disk for declared sequence')
    normalized=[];commands=[]
    direct=(resources['strategy']=='concat_demuxer' and width is None and height is None and fps is None)
    if not direct and len(paths)>1:
        raise MediaCompileError('media_normalization_required',
            'Incompatible streams require independent media.normalize_clip Tasks with the same width, height, fps and audio mode. '
            'Commit and validate each clip before joining their video roles with media.concat_sequence.')
    if direct:normalized=paths
    else:
        first=records[0]['signature']['video'];target_width=width or first['width'];target_height=height or first['height']
        if target_width%2 or target_height%2:raise ValueError('Output geometry must be even')
        target_fps=fps or first['r_frame_rate'] or '30'
        for index,(path,record) in enumerate(zip(paths,records)):
            # A normalization Task has exactly one input. Encode directly into
            # its final MP4 so AAC priming is represented by the MP4 edit list;
            # an MKV -> concat -> MP4 round trip adds ~21ms to every checkpoint.
            target=output
            command=['ffmpeg','-nostdin','-v','error','-y','-threads',str(threads),'-i',str(path)]
            if audio_mode=='preserve' and record['signature']['audio'] is None:
                command+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            command+=['-filter_threads',str(threads),'-map','0:v:0','-vf',f'scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},format=yuv420p',
                      '-c:v','libx264','-preset','veryfast','-crf',str(crf),'-threads',str(threads)]
            if audio_mode=='remove':command+=['-an']
            else:
                command+=['-map','0:a:0' if record['signature']['audio'] else '1:a:0','-c:a','aac','-ar','48000','-ac','2','-shortest']
            command+=[str(target)];_run(command,'media_normalization_failed');normalized.append(target)
            commands.append({'phase':'normalize','index':index,'argv_sha256':hashlib.sha256('\0'.join(command).encode()).hexdigest()})
    if direct:
        listing=workdir/'concat.txt';_concat_file(normalized,listing)
        command=['ffmpeg','-nostdin','-v','error','-y','-threads',str(threads),'-f','concat','-safe','0','-i',str(listing),'-map','0:v:0']
        if audio_mode!='remove':command+=['-map','0:a:0?']
        command+=['-c','copy',str(output)]
        _run(command,'media_concat_failed');commands.append({'phase':'concat','argv_sha256':hashlib.sha256('\0'.join(command).encode()).hexdigest()})
    return {'version':1,'resources':resources,'strategy':'normalize_single_clip' if not direct else 'concat_demuxer',
            'inputs':[{'sha256':file_digest(path),'duration':record['duration'],'signature':record['signature']} for path,record in zip(paths,records)],
            'commands':commands,'output':probe(output)}
def file_digest(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()
