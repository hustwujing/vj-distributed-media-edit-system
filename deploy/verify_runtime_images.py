#!/usr/bin/env python3
"""Run non-live, networkless contract checks against local runtime images."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess

try:
    from deploy.runtime_release import DEFAULT_OUTPUT,ROOT,write_json
except ModuleNotFoundError:
    from runtime_release import DEFAULT_OUTPUT,ROOT,write_json


REPORT=ROOT/'runtime/build/runtime-contract-report.json'


def docker_command(docker,tag,script):
    return [docker,'run','--rm','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--tmpfs','/workspace:rw,nosuid,nodev,size=64m,mode=0700,uid=10001,gid=10001',
        '--tmpfs','/state:rw,nosuid,nodev,size=64m,mode=0700,uid=10001,gid=10001',
        '--tmpfs','/run/video-use:rw,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001',
        '--entrypoint','/bin/sh',tag,'-eu','-c',script]


def verify(docker,manifest):
    checks={
        'operation-runtime:build':"""
test "$(id -u):$(id -g)" = 10001:10001
touch /workspace/probe /state/probe /run/video-use/probe
if touch /opt/rootfs-must-be-read-only 2>/dev/null; then exit 41; fi
python -c 'import cv2,opensandbox,webapp.runtime_bootstrap,webapp.image_tasks,helpers.canvas_render,grade,render,timeline_view,pack_transcripts,transcribe'
python -c 'from edit_captions import caption_font; path,family=caption_font(); assert path and family'
python -c 'import json; from webapp.runtime_bootstrap import verify_runtime_bundle; verify_runtime_bundle(json.load(open("/opt/video-use/bundle.json")))'
python -m webapp.runtime_bootstrap --help >/dev/null
ffmpeg -version >/dev/null 2>&1
mkdir -p /workspace/sources /workspace/edit
ffmpeg -v error -f lavfi -i color=red:s=64x64:r=25:d=2 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest /workspace/sources/a.mp4
ffmpeg -v error -f lavfi -i color=blue:s=64x64:r=25:d=2 -f lavfi -i sine=frequency=880:sample_rate=48000:duration=2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest /workspace/sources/b.mp4
printf '%s' '{"version":1,"sources":{"a":"sources/a.mp4","b":"sources/b.mp4"},"clips":[{"source":"a","end":2},{"source":"b","end":2}],"output":{"width":64,"height":64,"fps":25},"transition":{"type":"fade","duration":1}}' >/workspace/plan.json
python /opt/video-use/helpers/edit_media.py render /workspace/plan.json --workspace /workspace -o edit/xfade.mp4 >/dev/null
test "$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 /workspace/edit/xfade.mp4)" = 25/1
test "$(ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames -of default=nw=1:nk=1 /workspace/edit/xfade.mp4)" = 75
""",
        'agent-runtime-codex:build':"""
test "$(id -u):$(id -g)" = 10001:10001
if touch /opt/rootfs-must-be-read-only 2>/dev/null; then exit 41; fi
/opt/codex --version
python -c 'import webapp.runtime_bootstrap'
""",
        'agent-runtime-claude:build':"""
test "$(id -u):$(id -g)" = 10001:10001
if touch /opt/rootfs-must-be-read-only 2>/dev/null; then exit 41; fi
python -c 'import importlib.metadata as m,claude_agent_sdk,webapp.runtime_bootstrap; assert m.version("claude-agent-sdk")=="0.2.176"'
""",
        'agent-runtime-deepseek:build':"""
test "$(id -u):$(id -g)" = 10001:10001
if touch /opt/rootfs-must-be-read-only 2>/dev/null; then exit 41; fi
python -c 'import importlib.metadata as m,deepseek_harness,webapp.runtime_bootstrap; assert m.version("deepseek-harness-sdk")=="0.2.1rc2"'
""",
    }
    results={}
    for tag,script in checks.items():
        image=manifest['images'][tag]
        static=(image['user']=='10001:10001' and image['entrypoint']==
            ['/opt/venv/bin/python','-m','webapp.runtime_bootstrap'] and not image['secret_marker_findings'])
        result=subprocess.run(docker_command(docker,tag,script),capture_output=True,text=True,timeout=120)
        results[tag]={'passed':static and result.returncode==0,'static_contract':static,
            'exit_code':result.returncode,'stdout':result.stdout[-4000:],'stderr':result.stderr[-4000:]}
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
        'is_live_sandbox_test':False,'network':'none','read_only_root':True,
        'passed':all(value['passed'] for value in results.values()),'images':results}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--docker',default=os.environ.get('DOCKER','docker'))
    parser.add_argument('--manifest',type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument('--output',type=Path,default=REPORT);args=parser.parse_args()
    result=verify(args.docker,json.loads(args.manifest.read_text()));print(write_json(args.output,result))
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
