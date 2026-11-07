"""Operator-only read-only evidence and independent real artifact checks.
Only the explicitly named worker fault injection changes service state.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

REPO=Path(os.environ.get('DYNAMIC_CAMPAIGN_REPO',Path(__file__).resolve().parents[1])).resolve();sys.path.insert(0,str(REPO))
from deploy import dynamic_cluster as cluster
DEPLOY_ROOT=Path(os.environ.get('DYNAMIC_CASE_DEPLOY_ROOT',REPO/'.cluster')).resolve()
ROOT=DEPLOY_ROOT/'campaigns/dynamic-plan'
release=json.loads((cluster.ROOT/'release.json').read_text());sys.path.insert(0,release['source'])
os.environ.update(cluster.environment());os.environ.update(cluster.env_file(cluster.CONF/'observation.env'))
from webapp.store import Store
from webapp.diagnostics import diagnose
from webapp.media_isolation import run as isolated


def store():return Store(cluster.data('node','a'),initialize_schema=False,timeout=1)


def media(path,args,timeout=45):
    result=isolated(path,args,timeout)
    if result.returncode:raise ValueError('Isolated decoder failed: '+str(path.name))
    return result.stdout


def metadata(path):
    data=json.loads(media(path,['ffprobe','-v','error','-show_streams','-show_format','-of','json','/input']))
    streams=data.get('streams',[]);video=next((s for s in streams if s['codec_type']=='video'),None);audio=next((s for s in streams if s['codec_type']=='audio'),None)
    result={'duration':float(data.get('format',{}).get('duration',0)),'video':None,'audio':None}
    if video:
        n,d=map(int,video.get('avg_frame_rate','0/1').split('/'));result['video']={'width':video['width'],'height':video['height'],'fps':n/max(1,d),'codec':video['codec_name']}
    if audio:result['audio']={'sample_rate':int(audio['sample_rate']),'channels':audio['channels'],'codec':audio['codec_name']}
    return result


def frame(path,seconds):
    from PIL import Image
    # Output seeking decodes from the beginning. Input seeking can hide broken
    # H.264 parameter-set transitions at concatenation boundaries.
    raw=media(path,['ffmpeg','-v','error','-threads','2','-i','/input','-ss',str(seconds),'-frames:v','1','-threads','1','-f','image2pipe','-c:v','png','-'])
    return Image.open(io.BytesIO(raw)).convert('RGB')


def pcm(path):
    import numpy as np
    raw=media(path,['ffmpeg','-v','error','-threads','2','-i','/input','-vn','-ac','1','-ar','16000','-f','f32le','-'])
    return np.frombuffer(raw,dtype='<f4')


def image_info(path):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        if im.width*im.height>32_000_000:raise ValueError('Image exceeds QA decoder budget')
        rgba=np.asarray(im.convert('RGBA'));rgb=rgba[...,:3].astype(float);alpha=rgba[...,3]
        return {'width':im.width,'height':im.height,'format':im.format,'mode':im.mode,'transparent_fraction':float((alpha==0).mean()),
                'visible_fraction':float((alpha>=128).mean()),
                'mean':float(rgb.mean()),'channel_difference':float(np.max(rgb,axis=2).mean()-np.min(rgb,axis=2).mean()),
                'gradient':float(np.abs(np.diff(rgb,axis=0)).mean()+np.abs(np.diff(rgb,axis=1)).mean())}


def verify(run_dir):
    import numpy as np
    from PIL import Image,ImageDraw,ImageOps
    run_dir=Path(run_dir).resolve()
    if not run_dir.is_relative_to((ROOT/'runs').resolve()):raise ValueError('Invalid QA run directory')
    state=json.loads((run_dir/'state.json').read_text());spec=json.loads((run_dir/'case-definition.json').read_text())
    expected=spec['expect'];errors=[];items=[];images=[];videos=[];audios=[];texts=[];others=[]
    for artifact in state['artifacts']:
        path=Path(artifact['local_file']).resolve()
        if not path.is_relative_to(run_dir/'output') or not path.is_file():raise ValueError('Missing owned output')
        item={**artifact,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()};suffix=path.suffix.lower()
        eligible=artifact.get('workflow_delivery') is not False and artifact.get('quality_state') not in ('pending','superseded')
        item['eligible_delivery']=eligible
        if suffix in ('.png','.jpg','.jpeg','.webp'):
            item['image']=image_info(path)
            if eligible:images.append(item)
        elif suffix in ('.mp4','.mov','.mkv','.webm'):
            item['media']=metadata(path)
            if eligible:videos.append(item)
        elif suffix in ('.wav','.mp3','.m4a','.flac','.opus'):
            item['media']=metadata(path)
            if eligible:audios.append(item)
        elif suffix in ('.srt','.txt','.json','.md'):
            if path.stat().st_size<=2*1024**2:item['text']=path.read_text(errors='replace')
            if eligible:texts.append(item)
        else:
            if eligible:others.append(item)
        items.append(item)
    primary=next((a for a in items if a.get('is_primary')),None) or next((a for a in items if a['name']==state.get('primary_title')),None)
    kind=expected['kind'];selected=None
    if kind=='image':
        matches=[a for a in images if a['image']['width']==expected['width'] and a['image']['height']==expected['height']]
        if expected.get('alpha'):matches=[a for a in matches if a['image']['transparent_fraction']>0 and a['image']['visible_fraction']>0]
        selected=next((a for a in matches if a is primary),None) or (matches[0] if matches else None)
        if not selected:errors.append('No delivered image with required size/alpha')
        if len(images)<expected.get('min_images',1):errors.append('Missing delivered image variants')
        for width,height in expected.get('extra_image_sizes',[]):
            if not any((a['image']['width'],a['image']['height'])==(width,height) for a in images):errors.append(f'Missing image variant {width}x{height}')
        if selected and expected.get('extension'):
            allowed={'.jpg':{'JPEG'},'.webp':{'WEBP'}}.get(expected['extension'],{'PNG'})
            if selected['image']['format'] not in allowed:errors.append('Actual image format mismatch')
    elif kind in ('video','video_variants'):
        if kind=='video_variants':
            for w,h in expected['sizes']:
                if not any(a['media']['video'] and (a['media']['video']['width'],a['media']['video']['height'])==(w,h) for a in videos):errors.append(f'Missing video variant {w}x{h}')
            selected=primary if primary in videos else (videos[0] if videos else None)
            if selected and [selected['media']['video']['width'],selected['media']['video']['height']]!=expected['primary_size']:errors.append('Wrong primary video in UI')
        else:
            matches=[a for a in videos if a['media']['video'] and (a['media']['video']['width'],a['media']['video']['height'])==(expected['width'],expected['height'])]
            selected=next((a for a in matches if a is primary),None) or (matches[0] if matches else None)
        if not selected:errors.append('No delivered video with required dimensions')
        for item in videos if kind=='video_variants' else ([selected] if selected else []):
            m=item['media']
            if abs(m['duration']-expected['duration'])>expected.get('duration_tolerance',.2):errors.append('Video duration mismatch: '+item['name'])
            if expected.get('audio') is True and not m['audio']:errors.append('Missing audio stream')
            if expected.get('audio') is False and m['audio']:errors.append('Audio stream was not removed')
            if expected.get('audio_silent'):
                samples=pcm(Path(item['local_file']))
                item['audio_peak']=float(np.max(np.abs(samples))) if samples.size else None
                if samples.size==0 or item['audio_peak']>1e-4:errors.append('Audio track exists but decoded samples are not silent')
            if 'fps' in expected and abs(m['video']['fps']-expected['fps'])>.01:errors.append('Frame rate mismatch')
            try:
                progress=media(Path(item['local_file']),['ffmpeg','-v','error','-threads','2','-i','/input','-map','0:v:0','-fps_mode','passthrough','-progress','pipe:1','-f','null','-'],60)
                counts=re.findall(rb'(?m)^frame=(\d+)',progress)
                item['decoded_frames']=int(counts[-1]) if counts else 0
                if item['decoded_frames']<max(1,.98*m['duration']*m['video']['fps']):errors.append('Decoded video is shorter than its declared timeline')
            except ValueError as exc:errors.append(str(exc))
    elif kind=='audio':
        selected=primary if primary in audios else (audios[0] if audios else None)
        if not selected:errors.append('No delivered audio')
        if len(audios)<expected.get('min_audio',1):errors.append('Missing audio format variants')
        for item in audios:
            m=item['media']
            if abs(m['duration']-expected['duration'])>expected.get('duration_tolerance',.2):errors.append('Audio duration mismatch')
            if expected.get('channels') and m['audio']['channels']!=expected['channels']:errors.append('Audio channel count mismatch')
            if expected.get('sample_rate') and m['audio']['sample_rate']!=expected['sample_rate']:errors.append('Audio sample rate mismatch')
    elif kind=='text':
        for ext in expected['extensions']:
            if not any(Path(a['local_file']).suffix==ext for a in texts):errors.append('Missing delivered text format '+ext)
        content='\n'.join(a.get('text','') for a in texts).lower()
        for term in expected.get('terms',[]):
            if not re.search(r'\b'+re.escape(term.lower())+r'\b',content):errors.append('Missing transcript/OCR term '+term)
        if state['case']=='S30' and '-->' in content:errors.append('SRT timestamps were not removed')
        for item in texts:
            if Path(item['local_file']).suffix=='.srt':
                times=re.findall(r'(\d+):(\d+):(\d+),(\d+)',item.get('text',''))
                if not times:errors.append('No actual SRT timecodes')
                if any(int(h)*3600+int(m)*60+int(s)+int(ms)/1000>30.2 for h,m,s,ms in times):errors.append('SRT beyond input duration')
    if expected.get('extra_alpha') and not any(a['image']['transparent_fraction']>0 and a['image']['visible_fraction']>0 for a in images):errors.append('Requested transparent intermediate was not delivered')
    if expected.get('extra_image') and not images:errors.append('Requested image/contact sheet was not delivered')
    if expected.get('extra_audio') and not audios:errors.append('Requested audio was not delivered')
    if expected.get('extra_text') and not texts:errors.append('Requested text/report was not delivered')
    if expected.get('extra_extension'):
        ext=expected['extra_extension'];found=[a for a in items if a['eligible_delivery'] and Path(a['local_file']).suffix==ext]
        if not found:errors.append('Missing requested extra format '+ext)
        if ext=='.ora' and found:
            try:
                with zipfile.ZipFile(found[0]['local_file']) as archive:
                    from xml.etree import ElementTree as ET
                    names=archive.namelist()
                    if len(names)>100 or sum(i.file_size for i in archive.infolist())>256*1024**2:raise ValueError('ORA exceeds inspection budget')
                    if 'stack.xml' not in names or sum(n.startswith('data/') and n.endswith('.png') for n in names)<3:errors.append('ORA lacks required editable layers')
                    else:
                        xml=archive.read('stack.xml')
                        if len(xml)>1024*1024 or b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise ValueError('Unsafe ORA metadata')
                        document=ET.fromstring(xml);layers=list(document.iter('layer'));sources=[l.get('src','') for l in layers]
                        if len(set(sources))<3 or any(n not in names for n in sources):errors.append('ORA layer references are invalid')
                        else:
                            opaque=0;digests=set();canvas=Image.new('RGBA',(expected['width'],expected['height']))
                            for layer in reversed(layers):
                                data=archive.read(layer.get('src'));digests.add(hashlib.sha256(data).hexdigest())
                                im=Image.open(io.BytesIO(data))
                                if im.width*im.height>32_000_000:raise ValueError('Oversized ORA layer')
                                im=im.convert('RGBA')
                                if (np.asarray(im)[...,3]<16).mean()>.05:opaque+=1
                                opacity=float(layer.get('opacity','1'));im.putalpha(im.getchannel('A').point(lambda a:round(a*opacity)))
                                canvas.alpha_composite(im,(int(layer.get('x','0')),int(layer.get('y','0'))))
                            if len(digests)<3 or opaque<2:errors.append('ORA does not contain distinct transparent editable layers')
                            if selected:
                                reference=Image.open(selected['local_file']).convert('RGBA')
                                if reference.size==canvas.size and np.abs(np.asarray(reference,dtype=float)-np.asarray(canvas,dtype=float)).mean()>1:errors.append('ORA composition differs from delivered reconstruction')
            except (OSError,zipfile.BadZipFile):errors.append('Invalid ORA archive')
    checks={}
    fixture=ROOT/'fixtures'
    if state['case'] in ('S26','S30'):
        expected_text=' '.join(json.loads((fixture/'manifest.json').read_text())['speech_text'])
        actual_text=' '.join(a.get('text','') for a in texts if Path(a['local_file']).suffix=='.txt')
        def words(text):
            text=re.sub(r'(?im)^speaker[ _]?\d+\s*:', '',text)
            values=re.findall(r"[a-z0-9]+",text.lower());mapping={'1':'one','2':'two','3':'three','4':'four'}
            return [mapping.get(w,w) for w in values]
        wanted=words(expected_text);actual=words(actual_text);previous=list(range(len(actual)+1))
        for i,a in enumerate(wanted,1):
            current=[i]
            for j,b in enumerate(actual,1):current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+int(a!=b)))
            previous=current
        wer=previous[-1]/max(1,len(wanted));checks['normalized_word_error_rate']=wer
        if wer>(.15 if state['case']=='S26' else 0):errors.append('Text does not preserve the expected spoken/subtitle content')

    if selected and kind=='image' and state['case'] in ('S01','S02','S03','S04','S05','S06','S07','S11','S12'):
        source=Image.open(fixture/'portrait.png').convert('RGB');actual=Image.open(selected['local_file']).convert('RGB')
        transformations={'S01':lambda im:im.crop((128,128,384,384)),'S02':lambda im:im.resize((256,256),Image.Resampling.LANCZOS),
            'S03':lambda im:im.transpose(Image.Transpose.ROTATE_270),'S04':lambda im:im.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
            'S06':lambda im:ImageOps.grayscale(im).convert('RGB'),'S11':lambda im:im,'S12':lambda im:im.resize((256,256),Image.Resampling.LANCZOS)}
        if state['case']=='S05':reference=np.clip(np.asarray(source,dtype=float)*1.2,0,255)
        elif state['case'] in transformations:reference=np.asarray(transformations[state['case']](source),dtype=float)
        else:reference=None
        if reference is not None:
            mae=float(np.abs(np.asarray(actual,dtype=float)-reference).mean());checks['pixel_mae']=mae
            if mae>(9 if state['case'] in ('S02','S11','S12') else 4):errors.append('Image content does not match requested transformation')
        if state['case']=='S07':
            ratio=selected['image']['gradient']/image_info(fixture/'portrait.png')['gradient'];checks['gradient_ratio']=ratio
            if ratio>.75:errors.append('Gaussian blur effect insufficient')
    if selected and state['case']=='S08':
        rgba=Image.open(selected['local_file']).convert('RGBA');checks['logo_white_v']=rgba.getpixel((256,320))
        if checks['logo_white_v'][3]<200 or min(checks['logo_white_v'][:3])<220:errors.append('White V inside logo was erased or recolored')
    if selected and state['case'] in ('S14','S15'):
        actual=pcm(Path(selected['local_file']));original=pcm(fixture/'music.wav');length=min(len(actual),len(original));actual=actual[:length];original=original[:length]
        rms=lambda x:float(np.sqrt(np.mean(x*x)))
        if state['case']=='S14':
            ratio=rms(actual)/max(rms(original),1e-8);checks['rms_ratio']=ratio
            if not .44<ratio<.56:errors.append('Audio gain is not -6dB')
        else:
            checks['fade_start_ratio']=rms(actual[:3200])/max(rms(original[:3200]),1e-8);checks['fade_end_ratio']=rms(actual[-3200:])/max(rms(original[-3200:]),1e-8)
            if max(checks.values())>.5:errors.append('Audio edge fades insufficient')
    if selected and state['case']=='S13':
        actual=pcm(Path(selected['local_file']));original=pcm(fixture/'speech.wav')[32000:128000]
        n=min(len(actual),len(original));difference=float(np.abs(actual[:n]-original[:n]).mean());checks['waveform_mae']=difference
        if difference>.005:errors.append('Audio segment does not match source 2–8 seconds')
    if selected and state['case']=='S16':
        actual=pcm(Path(selected['local_file']));original=pcm(fixture/'clip_a.mp4');n=min(len(actual),len(original))
        correlation=float(np.corrcoef(actual[:n],original[:n])[0,1]);checks['audio_correlation']=correlation
        if not correlation>.95:errors.append('Extracted audio differs from the source')
    if selected and state['case']=='S20':
        sound=pcm(Path(selected['local_file']))[96000:105600]
        if len(sound)<1000:errors.append('Audio too short for pitch verification')
        else:
            spectrum=np.abs(np.fft.rfft(sound*np.hanning(len(sound))));freq=np.fft.rfftfreq(len(sound),1/16000)[int(np.argmax(spectrum))]
            checks['reference_tone_hz']=float(freq)
            if abs(freq-440)>8:errors.append('Playback speed changed the reference pitch')
    if selected and state['case'] in ('S17','S18','S19'):
        actual=frame(Path(selected['local_file']),0)
        original=frame(fixture/'clip_a.mp4',5 if state['case']=='S17' else 0)
        if state['case']=='S18':original=original.resize((320,180),Image.Resampling.LANCZOS)
        if state['case']=='S19':original=original.transpose(Image.Transpose.ROTATE_270)
        difference=float(np.abs(np.asarray(actual,dtype=float)-np.asarray(original,dtype=float)).mean());checks['first_frame_mae']=difference
        if difference>9:errors.append('Video content does not match the requested cut/resize/rotation')
    if state['case']=='S22':
        import itertools
        candidates=[a for a in images if (a['image']['width'],a['image']['height'])==(640,360)]
        references=[np.asarray(frame(fixture/'clip_a.mp4',t),dtype=float) for t in (0,5,10)]
        arrays=[np.asarray(Image.open(a['local_file']).convert('RGB'),dtype=float) for a in candidates]
        if len(arrays)>=3:
            scores=[[float(np.abs(a-r).mean()) for a in arrays] for r in references]
            assignment=min(itertools.permutations(range(len(arrays)),3),key=lambda p:sum(scores[i][p[i]] for i in range(3)))
            checks['timestamp_frame_mae']=[scores[i][assignment[i]] for i in range(3)]
            if max(checks['timestamp_frame_mae'])>6:errors.append('Extracted frames do not match 0, 5 and 10 seconds')
    if selected and state['case']=='S25':
        actual=np.asarray(frame(Path(selected['local_file']),.5),dtype=float)
        reference=frame(fixture/'clip_a.mp4',.5).convert('RGBA')
        logo=Image.open(fixture/'logo_rgba.png').convert('RGBA').resize((64,64),Image.Resampling.LANCZOS)
        reference.alpha_composite(logo,(560,16));expected_pixels=np.asarray(reference.convert('RGB'),dtype=float)
        checks['overlay_reference_mae']=float(np.abs(actual-expected_pixels).mean())
        if checks['overlay_reference_mae']>8:errors.append('Logo placement or underlying frame differs from required composition')
        green=(actual[...,1]>140)&(actual[...,1]>actual[...,0]+50)&(actual[...,1]>actual[...,2]+20)
        yy,xx=np.where(green)
        checks['green_marker_pixels']=len(xx)
        checks['green_marker_center']=[float(xx.mean()),float(yy.mean())] if len(xx) else None
        if len(xx)<10 or not (560<=xx.mean()<=624 and 16<=yy.mean()<=80):errors.append('Logo is not in the required top-right box')

    if selected and state['case']=='S24':
        values=np.asarray(frame(Path(selected['local_file']),2),dtype=float);difference=float((values.max(2)-values.min(2)).mean());checks['gray_channel_difference']=difference
        if difference>3:errors.append('Video is not grayscale')
    from campaign_complex_checks import check as complex_check
    extra_checks,extra_errors=complex_check(state['case'],selected,images,audios,fixture,frame,pcm)
    checks.update(extra_checks);errors.extend(extra_errors)
    if state['case'].startswith('R'):
        from campaign_dynamic_intervention_checks import check as intervention_check
        extra_checks,extra_errors=intervention_check(state,spec,images,videos,store(),fixture,frame,run_dir)
        checks.update(extra_checks);errors.extend(extra_errors)
    if state['case'].startswith('D'):
        from campaign_unified_checks import check as unified_check
        extra_checks,extra_errors=unified_check(state,images,videos,store(),frame)
        checks.update(extra_checks);errors.extend(extra_errors)
    # Contact sheet is independent evidence for required human/vision review.
    previews=[]
    if selected and kind in ('video','video_variants'):
        for seconds in (.5,expected['duration']/2,max(.5,expected['duration']-.5)):
            im=frame(Path(selected['local_file']),seconds);name=f'frame-{seconds:.2f}.png';im.save(run_dir/name);previews.append((name,im))
    for item in images[:8]:
        im=Image.open(item['local_file']).convert('RGBA');background=Image.new('RGBA',im.size,'#b8bec8');background.alpha_composite(im);previews.append((Path(item['name']).name,background.convert('RGB')))
    if previews:
        sheet=Image.new('RGB',(min(3,len(previews))*420,math.ceil(len(previews)/3)*330),'#eceff2');draw=ImageDraw.Draw(sheet)
        for i,(name,im) in enumerate(previews):
            im.thumbnail((404,295));x=(i%3)*420;y=(i//3)*330;sheet.paste(im,(x+(420-im.width)//2,y+25));draw.text((x+8,y+5),name[:50],fill='black')
        sheet.save(run_dir/'contact-sheet.jpg')
    if spec.get('allowed_external_operations'):
        db=store()
        calls=db.query("SELECT request_key,step,revision,provider,operation,state,external_id,network_seconds,poll_count FROM workflow_provider_calls WHERE workflow=? ORDER BY created",(state['run_id'],))
        checks['provider_receipts']=calls
        for operation in spec['allowed_external_operations']:
            if not any(c['operation']==operation and c['state']=='succeeded' for c in calls):errors.append('Missing successful durable provider receipt for '+operation)
        for call in calls:
            if call['operation'].startswith('video.') and call['state']=='succeeded' and (not call['external_id'] or not call['poll_count']):errors.append('Async video generation lacks task ID or polling evidence')
    tooling={'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'complex_checks_sha256':hashlib.sha256(Path(__file__).with_name('campaign_complex_checks.py').read_bytes()).hexdigest(),'application_release':release,'python':sys.version.split()[0]}
    summary=[{k:v for k,v in item.items() if k not in ('text',)} for item in items]
    return {'passed':not errors,'case':state['case'],'errors':errors,'checks':checks,'artifacts':summary,'primary':selected['name'] if selected else None,'content_review':'required','tooling':tooling,'at':time.time()}


def candidate(db,task):
    rows=db.query("SELECT a.id,a.worker,a.step_id,a.runtime,a.started,w.node FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker WHERE a.run_id=? AND a.kind='workflow' AND a.status='running' AND a.runtime<>'' ORDER BY a.started DESC",(task,))
    for row in rows:
        if time.time()-row['started']<2:continue
        specs=db.query('SELECT s.spec_json AS spec FROM run_tasks s JOIN runs w ON w.id=s.run_id WHERE w.id=? AND s.id=?',(task,row['step_id']))
        if not specs or not (json.loads(specs[0]['spec']).get('retry_policy')=='safe_once'):continue
        for node in cluster.PORTS:
            registry=cluster.registry(node)
            if (registry/'node-id').read_text().strip()!=row['node']:continue
            for file in (registry/'records').glob('*.json'):
                record=json.loads(file.read_text())
                if record.get('attempt')!=row['id']:continue
                for group in record.get('cgroups',[]):
                    try:
                        for pid in (Path(group)/'cgroup.procs').read_text().split():
                            if (Path('/proc')/pid/'comm').read_text().strip()=='ffmpeg':
                                ticks=int((Path('/proc')/pid/'stat').read_text().rsplit(')',1)[1].split()[19])
                                age=float(Path('/proc/uptime').read_text().split()[0])-ticks/os.sysconf('SC_CLK_TCK')
                                if age>=2:return {**row,'unit':f'{cluster.NAMESPACE}-executor@{node}.service','ready':True,'ffmpeg_pid':int(pid),'ffmpeg_start_ticks':ticks,'ffmpeg_runtime_seconds':age,'observed_at':time.time()}
                    except (OSError,ValueError):pass
    return {'ready':False}


def main():
    action,value=sys.argv[1:3]
    if os.geteuid()!=0:raise ValueError('Operator-only QA probe')
    if action=='archive':
        from campaign_archive import archive
        from webapp.diagnostics import observation
        result=archive(value,store(),observation)
    elif action=='verify':result=verify(value)
    elif action=='restore_worker':
        if value not in cluster.WORKERS:raise ValueError('Invalid worker')
        cluster.run(['systemctl','start',value]);result={'restored':value}
    else:
        if not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('Invalid task ID')
        db=store()
        if action=='task':
            result=diagnose(db,value,limit=200)
            records=db.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='tool' ORDER BY seq",(value,))
            latest={}
            for row in records:
                item=json.loads(row['payload'])
                if item.get('id'):latest[item['id']]=item
            intervals=[];commands=[]
            for item in latest.values():
                if item.get('status') not in ('completed','failed'):continue
                start=item.get('started');end=item.get('completed')
                if start is not None and end is not None and end>=start:intervals.append((start,end))
                commands.append({'id':item['id'],'category':item.get('category'),'status':item['status'],'duration_ms':item.get('duration_ms')})
            merged=[]
            for left,right in sorted(intervals):
                if merged and left<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],right)
                else:merged.append([left,right])
            tools_seconds=sum(b-a for a,b in merged)
            agent_seconds=sum(max(0,(a['finished'] or time.time())-a['started']) for a in result['attempts'] if a['kind']=='agent')
            result['qa_timing']={'agent_attempt_wall_seconds':round(agent_seconds,3),'tool_interval_union_seconds':round(tools_seconds,3),
                'agent_non_tool_seconds_estimate':round(max(0,agent_seconds-tools_seconds),3),
                'estimate_scope':'model communication/reasoning plus orchestration and setup; not pure inference or CPU time',
                'phase_metrics':result['metrics'],'tools':commands}
            result['qa_operation_budgets']=db.query('SELECT category,used FROM execution_budgets WHERE run_id=?',(value,))
            provider_steps={c['step'] for c in result.get('provider_calls',[]) if c['operation'].startswith('video.')}
            if provider_steps:
                records=db.query("SELECT step_id,status,started,finished,runtime FROM execution_attempts WHERE run_id=? AND kind='workflow' ORDER BY started",(value,))
                result['qa_video_attempts']=[{k:v for k,v in a.items() if k!='runtime'} | {'task_runtime_allocated':bool(a['runtime'])} for a in records if a['step_id'] in provider_steps]
                waits=[c for c in result['provider_calls'] if c['step'] in provider_steps and c['state'] in ('waiting','running','queued')]
                if waits and len(sys.argv)>3:
                    target=Path(sys.argv[3]).resolve().parent
                    if target.is_relative_to((ROOT/'runs').resolve()):
                        leases=db.query('SELECT slot FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(value,db.now()))
                        snapshot={'at':time.time(),'task':value,'provider_calls':waits,'active_local_leases':len(leases),'attempts':result['qa_video_attempts']}
                        with (target/'async-resource-samples.jsonl').open('a') as out:out.write(json.dumps(snapshot,ensure_ascii=False)+'\n')

        elif action=='fault_status':
            from campaign_fault import read
            result=read(ROOT/'fault-injections',value)
        elif action in ('restart_candidate','interrupt_worker'):
            result=candidate(db,value)
            if action=='interrupt_worker' and result['ready']:
                task=db.query('SELECT p.name FROM runs t JOIN projects p ON p.id=t.project_id WHERE t.id=?',(value,))
                if not task or not task[0]['name'].startswith('REAL60-C29-'):raise ValueError('Only the designated fault case may interrupt a worker')
                others=db.query("SELECT id FROM runs WHERE id<>? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(value,))
                if others:raise ValueError('Other business tasks are active')
                healthy=db.query('SELECT DISTINCT node FROM execution_workers WHERE draining=0 AND heartbeat>?',(db.now()-20,))
                if len({row['node'] for row in healthy})<2:
                    raise ValueError('Cross-node fault test requires at least two healthy distinct nodes')
                result['healthy_nodes_before_fault']=[row['node'] for row in healthy]
                from campaign_fault import stop_once
                result=stop_once(ROOT/'fault-injections',value,result,cluster.run)
        else:raise ValueError('Unknown action')
    if len(sys.argv)>3:
        path=Path(sys.argv[3]);path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
        print(json.dumps({'saved':str(path),'complete':result.get('complete')}))
    else:print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
