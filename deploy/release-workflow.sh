#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
release_mode="${1:---check}"
case "$release_mode" in --check|--prepare|--deploy) ;; *) echo 'Usage: release-workflow.sh --check|--prepare|--deploy' >&2; exit 2;; esac
.venv/bin/python - <<'PY'
import json
from pathlib import Path
items=json.loads(Path('docs/workflow-implementation-checklist.json').read_text())
blocked=[x['id'] for x in items if x['status'] not in ('passed','not_applicable')]
if blocked:raise SystemExit('Release gate blocked by checklist items: '+', '.join(blocked))
for item in items:
    if item['status']=='passed' and not all(item.get(k) for k in ('implementation','static_review','unit_tests','functional_tests')):
        raise SystemExit('Missing evidence: '+item['id'])
result=json.loads(Path('docs/test-evidence/overall-result.json').read_text())
if result.get('passed') is not True:raise SystemExit('Overall tests have not passed')
from tests.run_workflow_suite import source_digest
if result.get('source_sha256')!=source_digest():raise SystemExit('Source changed after overall tests')
print('Checklist and overall test gates passed.')
PY
if [ "$release_mode" = '--check' ]; then exit 0; fi
if [ "$(id -u)" != 0 ]; then echo 'Deployment requires the local service operator identity.' >&2; exit 1; fi
release_backup="/var/backups/video-use/workflow-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$release_backup"
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$release_backup/source.tar.gz" webapp helpers references runtime/public deploy docs pyproject.toml uv.lock
chmod 0600 "$release_backup/source.tar.gz"
.venv/bin/python - "$release_backup" <<'PY'
import sqlite3,sys
from pathlib import Path
backup=Path(sys.argv[1]);source=Path('workspace/state.sqlite3')
if source.exists():
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as original,sqlite3.connect(backup/'state.sqlite3') as target:original.backup(target)
    (backup/'state.sqlite3').chmod(0o600)
PY
for service in video-use.service video-use-agent.service video-use-reaper.service; do
    systemctl cat "$service" > "$release_backup/$service.previous" 2>/dev/null || true
done
printf 'Prepared local backup: %s\n' "$release_backup"
if [ "$release_mode" = '--prepare' ]; then exit 0; fi
# Stop accepting new API writes; the launcher drains current attempts on SIGTERM.
systemctl stop video-use.service
systemctl stop video-use-agent.service
bash deploy/prepare-executor.sh --models
.venv/bin/python - <<'PY'
from pathlib import Path
from webapp.store import Store
from webapp.agent_store import initialize
initialize(Store(Path('workspace')))
PY
install -m 0644 deploy/video-use.service /etc/systemd/system/video-use.service
install -m 0644 deploy/video-use-agent.service /etc/systemd/system/video-use-agent.service
install -m 0644 deploy/video-use-reaper.service /etc/systemd/system/video-use-reaper.service
# Authorize only the operator-configured internal model host; never print secrets.
.venv/bin/python - <<'PY'
from pathlib import Path
import shlex
from urllib.parse import urlsplit
path=Path('.agent.env');text=path.read_text();values={}
for line in text.splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1);parts=shlex.split(value);values[key]=parts[0] if parts else ''
if 'VIDEO_USE_INTERNAL_PROVIDER_HOSTS' not in values:
    hosts={'localhost','127.0.0.1','::1'}
    for key in ('VIDEO_USE_MODEL_BASE_URL','VIDEO_USE_IMAGE_GENERATION_URL','VIDEO_USE_GENERATION_URL','VIDEO_USE_IMAGE_VIDEO_URL'):
        host=urlsplit(values.get(key,'')).hostname
        if host:hosts.add(host)
    with path.open('a') as stream:stream.write('\nVIDEO_USE_INTERNAL_PROVIDER_HOSTS='+','.join(sorted(hosts))+'\n')
PY
systemctl daemon-reload
systemctl enable video-use-reaper.service
systemctl restart video-use-reaper.service
systemctl start video-use.service
systemctl start video-use-agent.service
.venv/bin/python - <<'PY'
import json,time,requests,subprocess
client=requests.Session();client.trust_env=False
for _ in range(30):
    try:
        response=client.get('http://127.0.0.1:18765/api/agent/health',timeout=2)
        if response.ok and response.json().get('available'):break
    except requests.RequestException:pass
    time.sleep(1)
else:raise SystemExit('Deployment health check failed; inspect services and retained backup before restoring.')
services=['video-use.service','video-use-agent.service','video-use-reaper.service']
for service in services:subprocess.run(['systemctl','is-active','--quiet',service],check=True)
print(json.dumps({'services':'active','api_health':'available','model_health':response.json().get('model_health','unverified')}))
PY
