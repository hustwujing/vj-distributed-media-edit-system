"""Deterministic, model-free media delivery checks.

This is deliberately conservative: it reports measurable signals for a human or
the Agent to inspect; it does not claim to judge story or aesthetics.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=180)
    if result.returncode:raise RuntimeError((result.stderr or result.stdout)[-3000:])
    return result.stderr+'\n'+result.stdout


def _events(path, filter_name, expression, keys):
    text=_run(['ffmpeg','-hide_banner','-v','info','-nostdin','-i',str(path),'-af' if filter_name=='silence' else '-vf',expression,'-f','null','-'])
    if filter_name=='silence':
        starts=[float(v) for v in re.findall(r'silence_start:\s*([\d.]+)',text)]
        ends=[float(v) for v in re.findall(r'silence_end:\s*([\d.]+)',text)]
        return [{'start':round(a,3),'end':round(b,3),'duration':round(b-a,3)} for a,b in zip(starts,ends) if b>a]
    if filter_name=='black':
        return [{'start':round(float(a),3),'end':round(float(b),3),'duration':round(float(duration),3)}
                for a,b,duration in re.findall(r'black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)',text)]
    return [{'start':round(float(a),3),'end':round(float(b),3),'duration':round(float(b)-float(a),3)}
            for a,b in re.findall(r'freeze_start: ([\d.]+).*?freeze_end: ([\d.]+)',text,re.S)]


def inspect(path:Path, metadata:dict):
    duration=float(metadata['duration']);issues=[]
    black=_events(path,'black','blackdetect=d=0.08:pic_th=0.98',())
    frozen=_events(path,'freeze','freezedetect=n=-60dB:d=1.5',())
    silence=[]
    if metadata.get('audio_tracks'):
        silence=_events(path,'silence','silencedetect=n=-50dB:d=0.7',())
    for label,items in [('黑帧',black),('静帧',frozen),('静音',silence)]:
        for item in items:
            if item['duration']>=.7 and item['start']>.02 and item['end']<duration-.02:
                issues.append({'type':label,**item,'severity':'review'})
    # Stream structure is a delivery requirement, unlike subjective image quality.
    checks=[{'name':'时长','status':'pass','detail':f'{duration:.3f} 秒'},
            {'name':'视频轨','status':'pass' if metadata.get('kind')=='video' else 'not_applicable'},
            {'name':'音频轨','status':'pass' if metadata.get('audio_tracks') else 'warning',
             'detail':'检测到音频轨' if metadata.get('audio_tracks') else '没有音频轨'}]
    if not metadata.get('audio_tracks'):issues.append({'type':'无音频轨','severity':'warning'})
    return {'version':1,'model_required':False,'duration':duration,'checks':checks,
            'signals':{'black_frames':black,'frozen_frames':frozen,'silence':silence},
            'issues':issues,
            'summary':'未发现需要人工复核的确定性信号。' if not issues else f'发现 {len(issues)} 个需要复核的确定性信号。'}
