"""Isolated execution of a validated job using the upstream helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'helpers'))


def execute(manifest: dict) -> list[str]:
    import grade
    import pack_transcripts
    import render
    import timeline_view
    import transcribe

    project = Path(manifest['project_dir'])
    output = Path(manifest['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    edit = project / 'edit'
    p = manifest['payload']
    assets = {a['id']: a for a in manifest['assets']}
    kind = p['kind']
    from .media_handlers import HANDLERS, execute as execute_media
    if kind in HANDLERS:return execute_media(manifest)

    if kind == 'image':
        if os.environ.get('VIDEO_USE_ISOLATED') == '1' and p['image_task']['action'] != 'export':
            import base64
            from urllib.request import Request, urlopen
            request=Request('http://127.0.0.1:18766/image-tools',data=json.dumps(p['image_task']).encode(),headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=850) as response:result=json.load(response)
            files=[]
            for item in result['files']:
                if Path(item['name']).name != item['name']:raise ValueError('Invalid image output')
                (output/item['name']).write_bytes(base64.b64decode(item['data'],validate=True))
                files.append(item['name'])
            return files
        from .image_tasks import execute_image
        return execute_image(p['image_task'],assets,project,output)


    def source(asset_id):
        return project / 'sources' / assets[asset_id]['filename']

    def transcript(asset_id, track=0):
        return transcribe.transcript_path(edit, source(asset_id), track)

    if kind == 'transcribe':
        key = None if os.environ.get('VIDEO_USE_ISOLATED') == '1' else os.environ.get('ELEVENLABS_API_KEY') or transcribe.load_api_key()
        def one(asset_id):
            print(f"转写素材：{assets[asset_id]['name']}", flush=True)
            if os.environ.get('VIDEO_USE_ISOLATED') == '1':
                from urllib.request import Request,urlopen
                request=Request('http://127.0.0.1:18766/transcribe',data=json.dumps({'asset':asset_id,'audio_track':p['audio_track'],
                    'language':p['language'] or None,'speakers':p['speakers']}).encode(),headers={'Content-Type':'application/json'})
                with urlopen(request,timeout=1900) as response:result=json.load(response)
                return asset_id,Path(result['path'])
            return asset_id, transcribe.transcribe_one(
                source(asset_id), edit, key, language=p['language'] or None,
                num_speakers=p['speakers'], audio_track=p['audio_track'], verbose=True)
        artifacts = []
        failures = []
        with ThreadPoolExecutor(max_workers=p['workers']) as pool:
            futures = [pool.submit(one, asset) for asset in p['assets']]
            for future in as_completed(futures):
                try:
                    asset_id, path = future.result()
                    name = f'transcript_{asset_id}.json'
                    shutil.copyfile(path, output / name)
                    artifacts.append(name)
                except Exception as exc:
                    failures.append(str(exc))
        if failures:
            raise RuntimeError('部分转写失败；成功结果已缓存，可重试。\n' + '\n'.join(failures))
        text = []
        for filename in artifacts:
            data = json.loads((output / filename).read_text())
            asset_id = filename.removeprefix('transcript_').removesuffix('.json')
            text.extend([assets[asset_id]['name'], data.get('text', ''), '', '逐词时间戳（秒）'])
            for word in data.get('words', []):
                if word.get('type') == 'word' and word.get('start') is not None and word.get('end') is not None:
                    text.append(f"[{word['start']:06.2f}–{word['end']:06.2f}] "
                                f"{word.get('speaker_id') or 'speaker'}  {word.get('text', '')}")
            text.append('')
        (output / 'transcript.txt').write_text('\n'.join(text), encoding='utf-8')
        return ['transcript.txt', *artifacts]





    raise ValueError('Unknown worker action: '+str(kind))


if __name__ == '__main__':
    # Bound ffmpeg's automatic thread counts on large shared machines.
    if hasattr(os, 'sched_getaffinity'):
        cpus = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, cpus[:int(os.environ.get('VIDEO_USE_CPUS', '4'))])
    config = json.loads(Path(sys.argv[1]).read_text())
    try:
        result = execute(config)
        from .operation_result import Result
        Path(config['output_dir'], 'result.json').write_text(Result.media(config['payload']['kind'],result).model_dump_json())
    except Exception as exc:
        from .telemetry import write_child_error
        write_child_error(exc)
        if isinstance(exc,subprocess.CalledProcessError):print((exc.stderr or b'').decode(errors='replace')[-6000:], file=sys.stderr)
        raise
