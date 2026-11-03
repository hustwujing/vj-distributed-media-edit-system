"""Deterministic, bounded measurements shared by quality checks and reviewers."""
from __future__ import annotations

from array import array
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


def audio_evidence(path: str | Path, *, sample_seconds: float=120) -> dict:
    """Return bounded objective audio measurements without retaining PCM."""
    command=['ffmpeg','-nostdin','-v','info','-threads','1','-i',str(path),'-map','0:a:0',
             '-t',str(sample_seconds),'-af','ebur128=peak=true,volumedetect','-f','null','-']
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
    return {'audio_silent':peak is None or not math.isfinite(peak) or peak<=-60,
            'integrated_loudness_lufs':integrated,'mean_volume_db':mean,'peak_volume_db':peak,
            'measurement_seconds':sample_seconds}


def dominant_frequency(path: str | Path, *, sample_seconds: float=20, sample_rate: int=8000) -> float | None:
    """Estimate a stable tone by bounded zero crossings; intended for objective tone criteria."""
    command=['ffmpeg','-nostdin','-v','error','-threads','1','-i',str(path),'-map','0:a:0',
             '-t',str(sample_seconds),'-ac','1','-ar',str(sample_rate),'-f','f32le','pipe:1']
    run=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       timeout=30,check=False)
    if run.returncode or not run.stdout:return None
    values=array('f');values.frombytes(run.stdout)
    if not values:return None
    peak=max(abs(value) for value in values)
    if peak<1e-4:return None
    threshold=peak*.05;crossings=0;previous=0
    for value in values:
        sign=1 if value>threshold else -1 if value<-threshold else previous
        if previous and sign!=previous:crossings+=1
        previous=sign
    return crossings*sample_rate/(2*len(values)) if crossings else None
