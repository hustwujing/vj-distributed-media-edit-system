"""Deterministic, bounded measurements shared by quality checks and reviewers."""
from __future__ import annotations

import math
from pathlib import Path
import re
import subprocess
import unicodedata


def normalize_text(value: str) -> str:
    value=unicodedata.normalize('NFC',value).replace('\r\n','\n').replace('\r','\n')
    return '\n'.join(' '.join(line.split()) for line in value.split('\n')).strip()


def text_matches(value: str, expected: list[str], mode: str) -> bool:
    if mode=='contains':return all(item in value for item in expected)
    if mode=='exact_text':return value==expected[0]
    if mode=='normalized_text':return normalize_text(value)==normalize_text(expected[0])
    if mode=='line_sequence':
        actual=[normalize_text(line) for line in value.splitlines() if normalize_text(line)]
        wanted=[normalize_text(line) for item in expected for line in item.splitlines() if normalize_text(line)]
    elif mode=='token_sequence':
        tokens=lambda text:re.findall(r'\w+|[^\w\s]',unicodedata.normalize('NFC',text),re.UNICODE)
        actual=tokens(value);wanted=[token for item in expected for token in tokens(item)]
    else:raise ValueError('Unsupported text match mode')
    return bool(wanted) and any(actual[index:index+len(wanted)]==wanted for index in range(len(actual)-len(wanted)+1))


def audio_channel_layout(stream: dict) -> dict:
    declared=stream.get('channel_layout')
    if declared and declared!='unknown':
        return {'channel_layout':declared,'channel_layout_source':'metadata','declared_channel_layout':declared}
    # Ordinary mono/stereo PCM headers need not carry a separate layout name.
    # Three or more channels do not uniquely identify a speaker arrangement.
    layout={1:'mono',2:'stereo'}.get(stream.get('channels'))
    return {'channel_layout':layout,'channel_layout_source':'channel_count' if layout else 'unknown',
            'declared_channel_layout':declared}


def alpha_evidence(image) -> dict:
    """Measure mask topology on a bounded 256px representation."""
    alpha=image.convert('RGBA').getchannel('A')
    pixels=image.width*image.height;histogram=alpha.histogram()
    result={'transparent_fraction':sum(histogram[:255])/pixels,
            'visible_fraction':sum(histogram[1:])/pixels,
            'opaque_fraction':histogram[255]/pixels,
            'partial_alpha_fraction':sum(histogram[1:255])/pixels}
    sample=alpha.copy();sample.thumbnail((256,256))
    width,height=sample.size;mask=[value>=128 for value in sample.getdata()]
    if not any(mask):return {**result,'alpha_components':0,'alpha_hole_fraction':0.0,'alpha_edge_fraction':0.0}
    seen=set();components=[]
    for start,foreground in enumerate(mask):
        if not foreground or start in seen:continue
        stack=[start];seen.add(start);size=0
        while stack:
            current=stack.pop();size+=1;x=current%width;y=current//width
            for nxt in ((current-1 if x else -1),(current+1 if x+1<width else -1),
                        (current-width if y else -1),(current+width if y+1<height else -1)):
                if nxt>=0 and mask[nxt] and nxt not in seen:seen.add(nxt);stack.append(nxt)
        components.append(size)
    background_seen=set();holes=0
    for start,foreground in enumerate(mask):
        if foreground or start in background_seen:continue
        stack=[start];background_seen.add(start);size=0;touches_edge=False
        while stack:
            current=stack.pop();size+=1;x=current%width;y=current//width
            touches_edge|=x==0 or y==0 or x+1==width or y+1==height
            for nxt in ((current-1 if x else -1),(current+1 if x+1<width else -1),
                        (current-width if y else -1),(current+width if y+1<height else -1)):
                if nxt>=0 and not mask[nxt] and nxt not in background_seen:background_seen.add(nxt);stack.append(nxt)
        if not touches_edge:holes+=size
    edge=0
    for current,foreground in enumerate(mask):
        if not foreground:continue
        x=current%width;y=current//width
        neighbors=((current-1 if x else -1),(current+1 if x+1<width else -1),
                   (current-width if y else -1),(current+width if y+1<height else -1))
        if any(nxt<0 or not mask[nxt] for nxt in neighbors):edge+=1
    foreground=sum(mask)
    return {**result,'alpha_components':len(components),'alpha_hole_fraction':holes/max(1,foreground),
            'alpha_edge_fraction':edge/max(1,foreground)}


def audio_evidence(path: str | Path, *, sample_seconds: float|None=None) -> dict:
    """Measure the complete stream by default, with bounded time and log output."""
    if sample_seconds is not None and (not math.isfinite(sample_seconds) or sample_seconds<=0):
        raise ValueError('Invalid audio sampling duration')
    command=['ffmpeg','-nostdin','-nostats','-hide_banner','-v','info','-threads','1','-i',str(path),'-map','0:a:0']
    if sample_seconds is not None:command+=['-t',str(sample_seconds)]
    command+=['-af','ebur128=peak=true:framelog=verbose,volumedetect','-f','null','-']
    run=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                       timeout=45,check=False)
    text=run.stderr.decode('utf-8','replace')[-256000:]
    if run.returncode:raise ValueError('Audio measurement failed')
    def number(pattern):
        matches=re.findall(pattern,text,re.I)
        if not matches:return None
        return float('-inf') if matches[-1].lower()=='-inf' else float(matches[-1])
    integrated=number(r'\bI:\s*(-?inf|-?\d+(?:\.\d+)?)\s+LUFS')
    peak=number(r'max_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s+dB')
    mean=number(r'mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s+dB')
    if peak is None or mean is None or integrated is None:raise ValueError('Audio measurement is incomplete')
    finite=lambda value:value if math.isfinite(value) else None
    return {'audio_silent':peak<=-60,'integrated_loudness_lufs':finite(integrated),
            'mean_volume_db':finite(mean),'peak_volume_db':finite(peak),
            'measurement_seconds':sample_seconds,'measurement_scope':'full' if sample_seconds is None else 'sampled'}


def mask_change_evidence(reference,candidate):
    """Compare full-resolution mask samples without resizing or semantic guesses."""
    import numpy as np
    from PIL import Image
    def load(path):
        with Image.open(path) as source:
            if source.width*source.height>32_000_000 or getattr(source,'n_frames',1)!=1:
                raise ValueError('Mask measurement resource limit')
            if 'A' in source.getbands():return np.array(source.getchannel('A'))
            rgb=np.array(source.convert('RGB'))
            if np.any(rgb[:,:,0]!=rgb[:,:,1]) or np.any(rgb[:,:,1]!=rgb[:,:,2]):
                raise ValueError('Mask reference must be alpha or grayscale')
            return rgb[:,:,0]
    before=load(reference);after=load(candidate)
    if before.shape!=after.shape:raise ValueError('Local mask repair cannot change geometry')
    changed=int(np.count_nonzero(before!=after));subject=int(np.count_nonzero(before>0))
    if not subject:raise ValueError('Local repair requires a nonempty reference subject')
    return {'changed_pixels':changed,'pixels':int(before.size),'reference_subject_pixels':subject,
            'changed_fraction':changed/subject,'canvas_changed_fraction':changed/before.size}


def dominant_frequency(path: str | Path, *, sample_seconds: float=20, sample_rate: int=8000) -> float | None:
    """Estimate a stable narrow-band tone; broadband or varying pitch is unknown."""
    if not math.isfinite(sample_seconds) or not 0<sample_seconds<=60 or type(sample_rate)!=int or not 4000<=sample_rate<=48000:
        raise ValueError('Tone measurement exceeds its sampling limits')
    command=['ffmpeg','-nostdin','-v','error','-threads','1','-i',str(path),'-map','0:a:0',
             '-t',str(sample_seconds),'-ac','1','-ar',str(sample_rate),'-f','f32le','pipe:1']
    run=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       timeout=30,check=False)
    if run.returncode or not run.stdout:return None
    import numpy as np
    values=np.frombuffer(run.stdout,dtype='<f4')
    if len(values)<512 or not np.isfinite(values).all() or np.max(np.abs(values))<1e-4:return None
    size=min(4096,len(values));window=np.hanning(size);frequencies=[];frames=0
    for start in range(0,len(values)-size+1,max(1,size//2)):
        frames+=1
        frame=values[start:start+size];spectrum=np.abs(np.fft.rfft((frame-frame.mean())*window))
        spectrum[0]=0;index=int(np.argmax(spectrum));energy=float(np.sum(spectrum**2))
        if index==0 or index>=len(spectrum)-1 or not energy:continue
        if np.sum(spectrum[max(1,index-2):index+3]**2)/energy<.5:continue
        left,middle,right=np.log(np.maximum(spectrum[index-1:index+2],1e-20))
        denominator=left-2*middle+right
        offset=.5*(left-right)/denominator if abs(denominator)>1e-12 else 0
        frequencies.append((index+float(np.clip(offset,-.5,.5)))*sample_rate/size)
    if not frequencies or len(frequencies)<.8*frames:return None
    center=float(np.median(frequencies))
    if np.percentile(np.abs(np.array(frequencies)-center),90)>max(5,center*.02):return None
    return center
