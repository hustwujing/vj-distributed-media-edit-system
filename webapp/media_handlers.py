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
