"""Fixed media handlers. The shared worker only dispatches registered actions."""
import json
import shutil
import subprocess
from pathlib import Path

HANDLERS = {}


def register(action):
    def decorate(handler):
        if action in HANDLERS:raise ValueError('Duplicate media handler: '+action)
        HANDLERS[action]=handler
        return handler
    return decorate


class Context:
    def __init__(self, manifest):
        self.project=Path(manifest['project_dir'])
        self.output=Path(manifest['output_dir'])
        self.output.mkdir(parents=True,exist_ok=True)
        self.parameters=manifest['payload']
        self.assets={a['id']:a for a in manifest['assets']}
        self.edit=self.project/'edit'
        self.input_roles=manifest.get('input_roles',{})
        self.explicit_inputs=manifest.get('explicit_inputs',False)
    def source(self, identifier):
        if identifier not in self.assets:raise ValueError('Undeclared input asset')
        return self.project/'sources'/self.assets[identifier]['filename']
    def transcript(self, identifier, track=0):
        if self.explicit_inputs:
            source_role=next((role for role,asset in self.input_roles.items() if asset==identifier),'source')
            reference=self.input_roles.get('transcript_'+source_role) or self.input_roles.get('transcript')
            return self.source(reference) if reference else self.output/'undeclared-transcript.json'
        import transcribe
        return transcribe.transcript_path(self.edit,self.source(identifier),track)


def execute(manifest):
    action=manifest['payload']['kind']
    if action not in HANDLERS:raise ValueError('Unknown fixed media action: '+action)
    return HANDLERS[action](Context(manifest))



@register('edit')
def edit(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    from .edit_tools import bind_plan
    from edit_media import render_plan
    declared_transcripts=({(clip['asset'],clip.get('audio_track',0)):ctx.transcript(clip['asset'],clip.get('audio_track',0))
                          for clip in p['edit_plan'].get('clips',[])} if ctx.explicit_inputs else None)
    plan,normalized=bind_plan(p['edit_plan'],assets,project,transcript_paths=declared_transcripts)
    (output/'edit_plan.json').write_text(json.dumps(p['edit_plan'],ensure_ascii=False,indent=2))
    result=output/('final.'+normalized['output']['format'])
    render_plan(normalized,result)
    return [result.name,*sorted(path.name for path in output.iterdir()
                               if path.name.startswith('final.') and path!=result),'edit_plan.json']


@register('pack')
def pack(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    paths = [source(identifier) for identifier in p['assets']] if ctx.explicit_inputs else sorted((edit / 'transcripts').glob('*.json'))
    if not paths:
        raise ValueError('请先完成至少一个素材的转写。')
    entries = [pack_transcripts.pack_one_file(path, p['silence']) for path in paths]
    markdown = pack_transcripts.render_markdown(entries, p['silence'])
    for asset_id, asset in assets.items():
        markdown = markdown.replace(asset_id, asset['name'])
    (output / 'takes_packed.md').write_text(markdown, encoding='utf-8')
    return ['takes_packed.md']


@register('timeline')
def timeline(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    asset_id = p['assets'][0]
    path = transcript(asset_id, p['audio_track'])
    timeline_view.render_timeline(source(asset_id), p['start'], p['end'],
                                  output / 'timeline.png', p['frames'],
                                  path if path.exists() else None)
    return ['timeline.png']


@register('analyze')
def analyze(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    asset_id = p['assets'][0]
    chain, stats = grade.auto_grade_for_clip(source(asset_id), start=p['start'],
                                            duration=p['end'] - p['start'])
    (output / 'analysis.json').write_text(json.dumps(
        {'素材': assets[asset_id]['name'], '建议滤镜': chain, '测量结果': stats},
        ensure_ascii=False, indent=2), encoding='utf-8')
    return ['analysis.json']


@register('review')
def review(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    from .quality_review import inspect
    asset_id=p['assets'][0]
    report=inspect(source(asset_id),assets[asset_id]['metadata'])
    report['素材']=assets[asset_id]['name']
    (output/'quality-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return ['quality-review.json']


@register('render')
def render_media(ctx):
    import grade, pack_transcripts, render, timeline_view
    project, output, edit = ctx.project, ctx.output, ctx.edit
    p, assets = ctx.parameters, ctx.assets
    source, transcript = ctx.source, ctx.transcript
    # Build a trusted EDL exclusively from uploaded assets and numeric ranges.
    edl = {'version': 1, 'sources': {}, 'ranges': [], 'grade': p['grade'], 'overlays': []}
    (output / 'transcripts').mkdir(exist_ok=True)
    for seg in p['segments']:
        asset_id, track = seg['asset'], seg['audio_track']
        alias = f'{asset_id}_t{track}'
        if alias not in edl['sources']:
            src = source(asset_id)
            # Give silent sources a silent track and select the requested track.
            # The video stream is copied; upstream still encodes each cut once.
            normalized = output / f'source_{alias}.mkv'
            audio = assets[asset_id]['metadata'].get('audio_tracks', [])
            cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(src)]
            if audio:
                cmd += ['-map', '0:v:0', '-map', f'0:a:{track}', '-c', 'copy', str(normalized)]
            else:
                cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
                        '-t', str(assets[asset_id]['metadata']['duration']), str(normalized)]
            subprocess.run(cmd, check=True, capture_output=True)
            edl['sources'][alias] = str(normalized)
            tr = transcript(asset_id, track)
            if tr.exists():
                shutil.copyfile(tr, output / 'transcripts' / f'{alias}.json')
        edl['ranges'].append({'source': alias, 'start': seg['start'], 'end': seg['end']})
    for overlay in p['overlays']:
        edl['overlays'].append({'file': str(source(overlay['asset'])),
                                'start_in_output': overlay['start'], 'duration': overlay['duration']})
    if p['subtitles'] == 'upload':
        shutil.copyfile(source(p['subtitle_asset']), output / 'master.srt')
        edl['subtitles'] = 'master.srt'
    if p['subtitles'] == 'transcript':
        render.build_master_srt(edl, output, output / 'master.srt')
        edl['subtitles'] = 'master.srt'
    (output / 'edl.json').write_text(json.dumps(edl, indent=2), encoding='utf-8')
    segments = render.extract_all_segments(edl, output, preview=p['quality'] == 'preview',
                                            draft=p['quality'] == 'draft', fps=p['fps'] or None)
    render.concat_segments(segments, output / 'base.mp4', output)
    subtitles = output / 'master.srt' if edl.get('subtitles') else None
    composite = output / ('composite.mp4' if p['normalize_audio'] else 'final.mp4')
    render.build_final_composite(output / 'base.mp4', edl['overlays'], subtitles, composite, output)
    if p['normalize_audio']:
        render.apply_loudnorm_two_pass(composite, output / 'final.mp4', preview=False)
    # Share a portable edit plan with asset IDs, without server filesystem paths.
    public_plan = {**p, 'source_names': {key: value['name'] for key, value in assets.items()}}
    (output / 'edit_plan.json').write_text(json.dumps(public_plan, ensure_ascii=False, indent=2))
    artifacts = ['final.mp4', 'edit_plan.json']
    if subtitles:
        artifacts.append('master.srt')
    # Successful jobs retain final files only, avoiding several copies of long videos.
    for path in output.glob('source_*.mkv'):
        path.unlink()
    for name in ('base.mp4', 'composite.mp4'):
        (output / name).unlink(missing_ok=True)
    for name in ('clips_draft', 'clips_preview', 'clips_graded', 'transcripts'):
        shutil.rmtree(output / name, ignore_errors=True)
    return artifacts


@register('concat_sequence')
def concat_sequence(ctx):
    from .media_plan_compiler import compile_sequence
    roles=ctx.parameters['roles']
    if any(role not in ctx.input_roles for role in roles):raise ValueError('Sequence references an undeclared input role')
    paths=[ctx.source(ctx.input_roles[role]) for role in roles]
    report=compile_sequence(paths,ctx.output/'final.mp4',ctx.output/'compiler-work',
        audio_mode=ctx.parameters['audio_mode'],threads=ctx.parameters['threads'])
    (ctx.output/'compiler-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    shutil.rmtree(ctx.output/'compiler-work',ignore_errors=True)
    return ['final.mp4','compiler-report.json']


@register('normalize_clip')
def normalize_clip(ctx):
    from .media_plan_compiler import compile_sequence
    if set(ctx.input_roles)!={'source'}:raise ValueError('Normalization requires exactly one source video')
    p=ctx.parameters
    report=compile_sequence([ctx.source(ctx.input_roles['source'])],ctx.output/'final.mp4',ctx.output/'compiler-work',
        **{key:p[key] for key in ('width','height','fps','crf','audio_mode','threads')})
    (ctx.output/'compiler-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    shutil.rmtree(ctx.output/'compiler-work',ignore_errors=True)
    return ['final.mp4','compiler-report.json']


def _atomic_report(path,payload):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2));temporary.replace(path)


@register('audio_process')
def audio_process(ctx):
    import re
    from .media_evidence import audio_evidence
    p=ctx.parameters;role=p['role']
    if set(ctx.input_roles)!={role}:raise ValueError('Audio process requires exactly its declared role')
    source=ctx.source(ctx.input_roles[role]);report_path=ctx.output/'audio-process-report.json'
    report={'version':2,'status':'measuring','parameters':{k:v for k,v in p.items() if k not in ('kind','assets')},'measurement':None,
            'normalization_applied':p.get('target_lufs') is not None,'channel_policy':'preserve','sample_rate_policy':'preserve'}
    _atomic_report(report_path,report)
    probe_run=subprocess.run(['ffprobe','-v','error','-select_streams','a:0','-show_entries',
        'format=duration:stream=sample_rate,channels,channel_layout','-of','json',str(source)],capture_output=True,text=True,timeout=30)
    if probe_run.returncode:
        report.update(status='failed',error_code='audio_probe_failed');_atomic_report(report_path,report);raise ValueError('Audio input probe failed')
    info=json.loads(probe_run.stdout);stream=info['streams'][0]
    duration=float(info['format']['duration']);sample_rate=int(stream['sample_rate']);channels=int(stream['channels']);filters=[]
    report['source_audio']={'sample_rate':sample_rate,'channels':channels,'channel_layout':stream.get('channel_layout')}
    report['source_measurement']=audio_evidence(source)
    if p['highpass_hz']:filters.append(f'highpass=f={p["highpass_hz"]:g}')
    if p['denoise']:filters.append('afftdn=nf=-25')
    if p['gain_db']:filters.append(f'volume={p["gain_db"]:g}dB')
    if p['fade_in']:filters.append(f'afade=t=in:st=0:d={min(duration,p["fade_in"]):g}')
    if p['fade_out']:filters.append(f'afade=t=out:st={max(0,duration-p["fade_out"]):g}:d={min(duration,p["fade_out"]):g}')
    if p.get('target_lufs') is not None:
        first=','.join([*filters,f'loudnorm=I={p["target_lufs"]:g}:TP={p["true_peak_db"]:g}:LRA=11:print_format=json'])
        command=['ffmpeg','-nostdin','-hide_banner','-v','info','-threads',str(p['threads']),'-i',str(source),'-map','0:a:0','-af',first,'-f','null','-']
        measured=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        matches=re.findall(r'\{[^{}]+\}',measured.stderr,re.S)
        try:values=json.loads(matches[-1]);required=('input_i','input_tp','input_lra','input_thresh','target_offset');assert all(key in values for key in required)
        except (ValueError,IndexError,AssertionError):
            report.update(status='failed',error_code='loudness_measurement_failed');_atomic_report(report_path,report);raise ValueError('Two-pass loudness measurement failed')
        report['measurement']={key:values[key] for key in required}
        filters.append(f'loudnorm=I={p["target_lufs"]:g}:TP={p["true_peak_db"]:g}:LRA=11:measured_I={values["input_i"]}:measured_TP={values["input_tp"]}:measured_LRA={values["input_lra"]}:measured_thresh={values["input_thresh"]}:offset={values["target_offset"]}:linear=true')
    extension=p['format'];output=ctx.output/('final.'+extension)
    codec={'wav':['-c:a','pcm_s16le'],'mp3':['-c:a','libmp3lame','-b:a','192k'],'m4a':['-c:a','aac','-b:a','192k']}[extension]
    command=['ffmpeg','-nostdin','-v','error','-y','-threads',str(p['threads']),'-i',str(source),'-map','0:a:0','-vn']
    if filters:command+=['-af',','.join(filters)]
    command+=codec+['-ar',str(sample_rate),'-ac',str(channels),'-threads',str(p['threads']),str(output)]
    report['filters']=filters;_atomic_report(report_path,report)
    rendered=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if rendered.returncode:
        report.update(status='failed',error_code='audio_render_failed',stderr_tail=rendered.stderr[-8000:]);_atomic_report(report_path,report);raise ValueError('Audio processing failed')
    report.update(status='succeeded',output=audio_evidence(output))
    if not any((p.get('target_lufs') is not None,p['highpass_hz'],p['denoise'],p['fade_in'],p['fade_out'])):
        before=report['source_measurement'];after=report['output']
        report['gain_measurement']={'scope':'full_stream_mean_and_peak','expected_gain_db':p['gain_db'],
            **{key:after[field]-before[field] if after[field] is not None and before[field] is not None else None
               for key,field in [('mean_delta_db','mean_volume_db'),('peak_delta_db','peak_volume_db')]}}
    _atomic_report(report_path,report)
    return [output.name,report_path.name]


@register('evidence')
def collect_evidence(ctx):
    from .media_evidence_operation import collect
    source=ctx.input_roles.get('source')
    if source not in ctx.assets:raise ValueError('Evidence source must be explicitly bound')
    reference=ctx.input_roles.get('reference')
    return collect(ctx.source(source),ctx.assets[source]['metadata']['kind'],ctx.output,ctx.parameters,
                   reference=ctx.source(reference) if reference else None)
