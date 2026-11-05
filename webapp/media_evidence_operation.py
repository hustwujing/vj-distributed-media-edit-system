"""Fixed evidence collection. Reports bind actual bytes and contain no verdict."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from fractions import Fraction


def collect(source,kind,output,parameters,*,reference=None):
    source=Path(source);output=Path(output)
    with source.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    params={'mode':parameters.get('mode','auto'),'offset':float(parameters.get('offset',0)),
            'window_seconds':float(parameters.get('window_seconds',8)),'crop':parameters.get('crop')}
    mode=params['mode'] if params['mode']!='auto' else {'video':'video_window','audio':'audio','image':'image'}.get(kind)
    report={'version':'verified-media-evidence-v1','source_sha256':sha,'parameters':params,'kind':mode}
    report['reference_sha256']=None
    if reference:
        with Path(reference).open('rb') as stream:report['reference_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    preview=output/'preview.jpg'
    if mode=='image':
        if kind!='image':raise ValueError('Image evidence requires an image input')
        from PIL import Image,ImageOps
        from .media_evidence import alpha_evidence
        with Image.open(source) as opened:
            if opened.width*opened.height>32_000_000 or getattr(opened,'n_frames',1)!=1:raise ValueError('Image evidence limit')
            image=ImageOps.exif_transpose(opened).convert('RGBA')
            report.update(width=image.width,height=image.height,alpha=alpha_evidence(image))
            if params['crop']:
                x1,y1,x2,y2=params['crop']
                if not (0<=x1<x2<=image.width and 0<=y1<y2<=image.height):raise ValueError('Evidence crop outside image')
                image=image.crop((x1,y1,x2,y2))
            background=Image.new('RGBA',image.size,'#b8bec8');background.alpha_composite(image)
            background.thumbnail((1024,1024));background.convert('RGB').save(preview,quality=85)
    elif mode=='video_window':
        if kind!='video':raise ValueError('Temporal evidence requires video')
        inspected=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(source)],capture_output=True,timeout=30,check=True)
        probe=json.loads(inspected.stdout);video=next(item for item in probe['streams'] if item['codec_type']=='video')
        duration=float(video.get('duration') or probe['format']['duration']);offset=params['offset']
        if not math.isfinite(duration) or not 0<=offset<duration:raise ValueError('Evidence window outside video')
        span=min(params['window_seconds'],duration-offset);count=16;rate=count/span
        crop=''
        if params['crop']:
            x1,y1,x2,y2=params['crop']
            if not (0<=x1<x2<=video['width'] and 0<=y1<y2<=video['height']):raise ValueError('Evidence crop outside video')
            crop=f'crop={x2-x1}:{y2-y1}:{x1}:{y1},'
        filters=(f'{crop}fps={rate:g},scale=256:160:force_original_aspect_ratio=decrease,pad=256:160:(ow-iw)/2:(oh-ih)/2,'
            f'setpts=PTS-STARTPTS+{offset:g}/TB,'
            "drawtext=font='DejaVu Sans':text='%{pts\\:hms}':x=4:y=4:fontsize=14:fontcolor=white:box=1:boxcolor=black@0.7,"
            f'showinfo,tile=4x{math.ceil(count/4)}:nb_frames={count}:padding=2:margin=2')
        result=subprocess.run(['ffmpeg','-nostdin','-nostats','-v','info','-y','-threads','1','-filter_threads','1',
            '-ss',str(offset),'-t',str(span),'-i',str(source),'-an','-sn','-dn','-vf',filters,'-frames:v','1','-q:v','5','-threads','1',str(preview)],
            capture_output=True,timeout=30,check=True)
        times=[float(value) for value in re.findall(rb'pts_time:([0-9.eE+-]+)',result.stderr)][:count]
        if len(times)<2:raise ValueError('Insufficient decoded temporal evidence')
        try:fps=float(Fraction(video.get('avg_frame_rate','0/1')))
        except (ValueError,ZeroDivisionError):fps=0
        report.update(coverage='contact_sheet',duration_seconds=duration,frame_timestamps=times,
                      source_frame_interval_seconds=1/fps if fps>0 else None)
        report['structure']=structure_metrics(source,offset,span,rate,count,crop,reference)
    elif mode in ('audio','audio_envelope','audio_compare'):
        if kind not in ('audio','video'):raise ValueError('Audio evidence requires an audio stream')
        from .media_evidence import audio_evidence
        report['measurements']=audio_evidence(source)
        if mode=='audio_envelope':
            probe=json.loads(subprocess.run(['ffprobe','-v','error','-show_format','-of','json',str(source)],
                capture_output=True,timeout=30,check=True).stdout)
            duration=float(probe['format']['duration']);offset=params['offset']
            if not 0<=offset<duration:raise ValueError('Audio envelope outside stream')
            span=min(params['window_seconds'],duration-offset);segments=min(8,max(2,math.ceil(span/.25)));width=span/segments
            report['envelope']=[audio_evidence(source,sample_seconds=width,offset_seconds=offset+index*width)
                                for index in range(segments)]
            report['envelope_interpretation']='time_local_signal_levels_not_a_semantic_fade_verdict'
        if mode=='audio_compare':
            if not reference:raise ValueError('Audio comparison requires a reference input')
            report['comparison']=audio_comparison(source,reference,params['offset'],params['window_seconds'])
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-threads','1','-filter_threads','1','-ss',str(params['offset']),
            '-t',str(params['window_seconds']),'-i',str(source),'-filter_complex','showwavespic=s=1024x256:colors=white',
            '-frames:v','1','-threads','1',str(preview)],capture_output=True,timeout=30,check=True)
        report['preview_coverage']='declared_audio_window'
    else:raise ValueError('Unsupported evidence mode')
    if not preview.is_file() or preview.stat().st_size>1024*1024:raise ValueError('Evidence preview exceeds limit')
    with source.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=sha:raise ValueError('Evidence source changed during measurement')
    with preview.open('rb') as stream:report['preview_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    (output/'evidence-report.json').write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,allow_nan=False))
    return ['evidence-report.json','preview.jpg']


def audio_comparison(source,reference,offset,window_seconds):
    """Bounded signal similarity evidence; it is not a source-separation verdict."""
    import numpy as np
    def decode(path):
        result=subprocess.run(['ffmpeg','-nostdin','-v','error','-threads','1','-ss',str(offset),'-t',str(window_seconds),
            '-i',str(path),'-map','0:a:0','-ac','1','-ar','8000','-f','f32le','pipe:1'],capture_output=True,timeout=30,check=True)
        if not result.stdout or len(result.stdout)>8*8000*4:raise ValueError('Audio comparison sample limit')
        return np.frombuffer(result.stdout,dtype='<f4').astype(np.float64)
    left=decode(source);right=decode(reference);count=min(len(left),len(right));left=left[:count];right=right[:count]
    if count<800:raise ValueError('Audio comparison window too short')
    left-=left.mean();right-=right.mean();left_rms=float(np.sqrt(np.mean(left*left)));right_rms=float(np.sqrt(np.mean(right*right)))
    denominator=float(np.linalg.norm(left)*np.linalg.norm(right))
    return {'sample_rate':8000,'samples':count,'offset_seconds':offset,'window_seconds':count/8000,
            'normalized_correlation':float(np.dot(left,right)/denominator) if denominator else None,
            'source_rms':left_rms,'reference_rms':right_rms,
            'rms_ratio_db':20*math.log10(left_rms/right_rms) if left_rms>0 and right_rms>0 else None,
            'interpretation':'bounded_signal_similarity_not_source_separation_or_identity'}


def structure_metrics(source,offset,span,rate,count,crop,reference):
    """Measured pixel/edge changes are evidence, never an identity verdict."""
    import numpy as np
    result=subprocess.run(['ffmpeg','-nostdin','-v','error','-threads','1','-filter_threads','1',
        '-ss',str(offset),'-t',str(span),'-i',str(source),'-an','-sn','-dn','-vf',
        f'{crop}fps={rate:g},scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2',
        '-frames:v',str(count),'-threads','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,timeout=30,check=True)
    frame_bytes=160*90*3
    if not result.stdout or len(result.stdout)%frame_bytes or len(result.stdout)>16*frame_bytes:raise ValueError('Invalid bounded frame samples')
    frames=np.frombuffer(result.stdout,dtype=np.uint8).reshape(-1,90,160,3).astype(np.float32)/255
    gray=frames.mean(axis=-1)
    edges=lambda data:(np.abs(np.diff(data,axis=-1,prepend=data[...,:1]))+np.abs(np.diff(data,axis=-2,prepend=data[...,:1,:])))/2
    edge=edges(gray)
    output={'method':'bounded-luma-and-edge-mae-v1','interpretation':'structural_change_not_identity_verdict',
        'frame_count':len(frames),'unique_sample_frames':len({hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames}),'adjacent_luma_mae':np.mean(np.abs(np.diff(gray,axis=0)),axis=(1,2)).tolist(),
        'adjacent_edge_mae':np.mean(np.abs(np.diff(edge,axis=0)),axis=(1,2)).tolist()}
    if reference:
        from PIL import Image,ImageOps
        with Image.open(reference) as opened:
            if opened.width*opened.height>32_000_000:raise ValueError('Reference evidence limit')
            image=ImageOps.contain(ImageOps.exif_transpose(opened).convert('RGB'),(160,90))
            canvas=Image.new('RGB',(160,90),'black');canvas.paste(image,((160-image.width)//2,(90-image.height)//2))
            ref=np.array(canvas,dtype=np.float32).mean(axis=-1)/255
        output['reference_alignment']='contain_black_padding_160x90'
        output['reference_luma_mae']=np.mean(np.abs(gray-ref),axis=(1,2)).tolist()
        output['reference_edge_mae']=np.mean(np.abs(edge-edges(ref)),axis=(1,2)).tolist()
    return output
