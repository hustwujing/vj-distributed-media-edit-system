"""Fixed checker inside a read-only task namespace. No network/model access."""
import hashlib
import json
import math
import re
from pathlib import Path
import resource
import tempfile
import os
import subprocess
import sys
from text_formats import read_text,validate_subtitle,FormatError,TEXT_SUFFIXES,SUBTITLE_SUFFIXES
from media_evidence import alpha_evidence,audio_evidence,audio_channel_layout,dominant_frequency,text_matches

resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024**2,2*1024**2))


def inspect(name):
    path=Path(name)
    if path.is_absolute() or '..' in path.parts or path.is_symlink() or not path.is_file():raise ValueError('无效产物路径')
    size=path.stat().st_size
    if not 0<size<=2*1024**3:raise ValueError('产物为空或过大')
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    value={'name':name,'size':size,'sha256':digest.hexdigest(),'kind':'file'}
    if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):
        from PIL import Image
        with Image.open(path) as image:
            if image.width*image.height>64_000_000:raise ValueError('图片尺寸过大')
            image.load()
            value.update(kind='image',width=image.width,height=image.height,has_alpha='A' in image.getbands() or 'transparency' in image.info)
            if value['has_alpha']:
                value.update(alpha_evidence(image))
            else:value.update(transparent_fraction=0.0,visible_fraction=1.0,opaque_fraction=1.0,
                              partial_alpha_fraction=0.0,alpha_components=1,alpha_hole_fraction=0.0,
                              alpha_edge_fraction=0.0)
        with Image.open(path) as image:image.verify()
    elif path.suffix.lower() in ('.mp4','.mov','.webm','.mkv','.mp3','.wav','.m4a','.flac','.ogg','.aac','.opus'):
        record=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',name],timeout=15))
        duration=float(record.get('format',{}).get('duration',0))
        if not math.isfinite(duration) or duration<=0 or not record.get('streams'):raise ValueError('媒体时长或轨道无效')
        video=next((s for s in record['streams'] if s.get('codec_type')=='video'),{})
        audio_streams=[s for s in record['streams'] if s.get('codec_type')=='audio']
        value.update(kind='media',duration=duration,width=video.get('width'),height=video.get('height'),
                     audio_track_present=bool(audio_streams),video=bool(video))
        value['audio_tracks']=[{**{key:stream.get(key) for key in ('index','codec_name','channels','sample_rate')},
                                **audio_channel_layout(stream)} for stream in audio_streams]
        value.update(audio_channel_layout(audio_streams[0]) if audio_streams else {'channel_layout':None})
        rate=video.get('avg_frame_rate','0/1').split('/')
        value['fps']=float(rate[0])/max(1,float(rate[1])) if len(rate)==2 else float(rate[0])
    elif path.suffix.lower()=='.json':
        if size>2*1024**2:raise ValueError('JSON exceeds limit')
        parsed=json.loads(path.read_text());value.update(kind='json',keys=list(parsed) if isinstance(parsed,dict) else [])
    elif path.suffix.lower() in SUBTITLE_SUFFIXES:
        # Downstream operations consume this attested type, not just the file
        # suffix or the success of an optional acceptance rule.
        value.update(kind='subtitle',**validate_subtitle(path))
    return value


def main():
    request=json.load(sys.stdin)
    files=request['files'];rules=request.get('acceptance',[])
    if len(files)>40 or len(rules)>20:raise ValueError('验收范围过大')
    report={'version':'quality-v1','technical_valid':True,'passed':True,'requires_review':False,'requires_agent':False,'files':[],'checks':[]}
    if not files:
        report['technical_valid']=False;report['passed']=False
        report['checks'].append({'passed':False,'code':'missing','reason':'没有输出文件'})
    for name in files:
        try:report['files'].append(inspect(name))
        except FormatError as exc:
            report['technical_valid']=False;report['passed']=False
            report['checks'].append({'file':name,'passed':False,'reason':str(exc),'code':exc.code,'codes':[exc.code]})
        except Exception:
            report['technical_valid']=False;report['passed']=False
            report['checks'].append({'file':name,'passed':False,'reason':'文件完整性或媒体格式检查失败','code':'integrity'})
    audio_cache={};tone_cache={}
    for index,rule in enumerate(rules):
        if rule['kind'] in ('review','agent'):
            targets=[f for f in report['files'] if (rule.get('file','*')=='*' or Path(f['name']).name==rule['file']) and (rule.get('target') not in ('image','media','json') or f['kind']==rule['target'])]
            if not targets:
                if rule.get('severity')!='signal':report['passed']=False
                report['checks'].append({'passed':False,'code':'missing','reason':'内容验收没有对应产物','criterion':index})
            report['requires_review' if rule['kind']=='review' else 'requires_agent']=True
            report['checks'].append({'passed':None,'reason':rule['criterion'],'kind':rule['kind'],'criterion':index})
            continue
        selected=[f for f in report['files'] if (rule.get('file','*')=='*' or Path(f['name']).name==rule['file'])
                  and (rule['kind'] not in ('image','media') or f['kind']==rule['kind'])]
        if rule.get('file','*')=='*' and rule['kind'] in ('text','subtitle','json'):
            allowed={'text':TEXT_SUFFIXES,'subtitle':SUBTITLE_SUFFIXES,'json':{'.json'}}[rule['kind']]
            selected=[f for f in selected if Path(f['name']).suffix.lower() in allowed]
        if not selected:
            if rule.get('severity')!='signal':report['passed']=False
            report['checks'].append({'passed':False,'reason':'没有符合验收条件的输出文件','code':'missing','criterion':index})
        if rule.get('checker'):
            try:
                checker=Path(rule['checker'])
                if checker.is_absolute() or '..' in checker.parts or checker.is_symlink() or not str(checker).startswith('edit/') or not checker.is_file():raise ValueError('Invalid checker')
                with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
                    run=subprocess.run([sys.executable,str(checker)],env={**os.environ,'VIDEO_USE_CHECK_FILES':json.dumps([x['name'] for x in selected])},stdout=output,stderr=errors,timeout=20)
                    output.seek(0);custom=json.loads(output.read(65536))
                if run.returncode or not isinstance(custom.get('passed'),bool) or not isinstance(custom.get('reason'),str):raise ValueError('Invalid checker report')
                report['checks'].append({'criterion':index,'passed':custom['passed'],'reason':custom['reason'][:2000],'code':'custom','severity':rule.get('severity','required')})
                if not custom['passed'] and rule.get('severity')!='signal':report['passed']=False
            except Exception:
                report['checks'].append({'criterion':index,'passed':False,'reason':'自定义检查器失败、超时或返回无效报告','code':'custom'})
                if rule.get('severity')!='signal':report['passed']=False
        for value in selected:
            failed=[];codes=[]
            for key in ('width','height','audio_track_present','channel_layout','video'):
                if rule.get(key) is not None and value.get(key)!=rule[key]:failed.append(key+' 不符合要求');codes.append(key)
            if rule.get('min_duration') is not None and value.get('duration',-1)<rule['min_duration']:failed.append('时长不足');codes.append('min_duration')
            if rule.get('max_duration') is not None and value.get('duration',float('inf'))>rule['max_duration']:failed.append('时长超出范围');codes.append('max_duration')
            if rule.get('max_bytes') is not None and value['size']>rule['max_bytes']:failed.append('文件大小超限');codes.append('max_bytes')
            if rule['kind']=='text':
                if value['size']>2*1024**2:failed.append('文本过大');codes.append('max_bytes')
                else:
                    try:
                        text=read_text(value['name'])
                        if not text_matches(text,rule.get('contains',[]),rule.get('text_match','contains')):
                            failed.append('文本没有按声明的匹配模式满足要求');codes.append('contains')
                    except FormatError as exc:failed.append(str(exc));codes.append(exc.code)
            if rule.get('has_alpha') is not None and value.get('has_alpha')!=rule['has_alpha']:failed.append('透明通道不符');codes.append('alpha')
            if rule.get('transparent_subject'):
                if not value.get('has_alpha') or value.get('transparent_fraction',0)<=0:failed.append('没有实际透明背景像素');codes.append('alpha')
                if value.get('visible_fraction',0)<=0:failed.append('没有可见主体像素');codes.append('alpha')
            if rule.get('min_transparent_fraction') is not None and value.get('transparent_fraction',-1)<rule['min_transparent_fraction']:failed.append('透明区域不足');codes.append('alpha')
            if rule.get('min_visible_fraction') is not None and value.get('visible_fraction',-1)<rule['min_visible_fraction']:failed.append('可见主体区域不足');codes.append('alpha')
            if rule.get('max_partial_alpha_fraction') is not None and value.get('partial_alpha_fraction',float('inf'))>rule['max_partial_alpha_fraction']:failed.append('半透明边缘区域过多');codes.append('alpha')
            if rule.get('max_alpha_components') is not None and value.get('alpha_components',float('inf'))>rule['max_alpha_components']:failed.append('蒙版连通区域过多');codes.append('alpha')
            if rule.get('max_alpha_hole_fraction') is not None and value.get('alpha_hole_fraction',float('inf'))>rule['max_alpha_hole_fraction']:failed.append('蒙版内部孔洞过多');codes.append('alpha')
            needs_audio=any(rule.get(key) is not None for key in ('audio_silent','min_loudness_lufs','max_loudness_lufs'))
            if needs_audio:
                if not value.get('audio_track_present'):failed.append('需要测量音频但产物没有音轨');codes.append('audio_track_present')
                else:
                    try:
                        measured=audio_cache.setdefault(value['name'],audio_evidence(value['name']))
                        value.update(measured)
                        if rule.get('audio_silent') is not None and measured['audio_silent']!=rule['audio_silent']:failed.append('静音状态不符');codes.append('audio_silent')
                        loudness=measured.get('integrated_loudness_lufs')
                        if rule.get('min_loudness_lufs') is not None and (loudness is None or loudness<rule['min_loudness_lufs']):failed.append('响度低于要求');codes.append('loudness')
                        if rule.get('max_loudness_lufs') is not None and (loudness is None or loudness>rule['max_loudness_lufs']):failed.append('响度高于要求');codes.append('loudness')
                    except Exception:failed.append('音频客观测量失败');codes.append('loudness')
            if rule.get('tone_hz') is not None:
                if not value.get('audio_track_present'):failed.append('需要测量音高但产物没有音轨');codes.append('audio_track_present')
                else:
                    try:
                        tone=tone_cache.setdefault(value['name'],dominant_frequency(value['name']))
                        value['dominant_frequency_hz']=tone
                        if tone is None or abs(tone-rule['tone_hz'])>rule.get('tone_tolerance_hz',5):failed.append('主频不符');codes.append('tone')
                    except Exception:failed.append('音高客观测量失败');codes.append('tone')
            if rule.get('fps') is not None and abs(value.get('fps',0)-rule['fps'])>.02:failed.append('帧率不符');codes.append('fps')
            if rule.get('decode'):
                try:
                    decoded=subprocess.run(['ffmpeg','-v','error','-xerror','-i',value['name'],'-progress','pipe:1','-nostats','-f','null','-'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,check=True)
                    import re
                    frames=re.findall(rb'(?m)^frame=(\d+)',decoded.stdout)
                    if value.get('video') and value.get('fps',0)>0:
                        value['decoded_frames']=int(frames[-1]) if frames else 0
                        if value['decoded_frames']<max(1,.98*value['duration']*value['fps']):raise ValueError('Decoded video is shorter than its declared duration')
                except Exception:failed.append('完整解码失败或超出检查时限');codes.append('decode')
            if rule['kind']=='json' and (value['kind']!='json' or not set(rule.get('required_keys',[])).issubset(value.get('keys',[]))):failed.append('JSON 结构不符');codes.append('schema')
            if rule['kind']=='subtitle':
                try:validate_subtitle(value['name'])
                except FormatError as exc:failed.append(str(exc));codes.append(exc.code)
            report['checks'].append({'file':value['name'],'severity':rule.get('severity','required'),'passed':not failed,'codes':codes,'criterion':index,'reason':'；'.join(failed) or '通过','actual':value,'expected':rule})
            if failed and rule.get('severity')!='signal':report['passed']=False
    if request.get('evidence_report'):
        expected=request['evidence_report']
        try:
            if expected['report_file'] not in files or expected['preview_file'] not in files:raise ValueError('Evidence outputs outside scope')
            if Path(expected['report_file']).stat().st_size>65536:raise ValueError('Evidence report exceeds limit')
            evidence=json.loads(Path(expected['report_file']).read_text())
            preview=next(item for item in report['files'] if item['name']==expected['preview_file'])
            if (evidence['version']!='verified-media-evidence-v1' or evidence['source_sha256']!=expected['source_sha256']
                or evidence.get('reference_sha256')!=expected['reference_sha256'] or evidence['parameters']!=expected['parameters'] or evidence['preview_sha256']!=preview['sha256']):
                raise ValueError('Evidence does not bind the actual source, parameters and preview')
            report['verified_evidence']={**evidence,**expected}
        except Exception as exc:
            report['technical_valid']=False;report['passed']=False
            report['checks'].append({'passed':False,'code':'evidence_integrity','error_type':type(exc).__name__,'reason':'证据报告未绑定实际输入和预览'})
    if request.get('mask_repair'):

        from media_evidence import mask_change_evidence
        rule=request['mask_repair']
        try:
            if rule['candidate'] not in files:raise ValueError('Mask candidate outside declared outputs')
            measured=mask_change_evidence(rule['reference'],rule['candidate'])
            passed=measured['changed_fraction']<=rule['max_changed_fraction']
            report['checks'].append({'passed':passed,'code':'mask_change_limit','actual':measured,
                'expected':{'max_changed_fraction':rule['max_changed_fraction']},
                'reason':'局部蒙版改动范围符合约定' if passed else '蒙版改动超出冻结的局部修复范围'})
        except Exception as exc:
            passed=False
            report['checks'].append({'passed':False,'code':'mask_measurement_unavailable','error_type':type(exc).__name__,
                'reason':'无法核实原蒙版与修复结果的变化范围'})
        if not passed:report['technical_valid']=False;report['passed']=False
    print(json.dumps(report,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':main()
