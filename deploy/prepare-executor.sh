#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installation step as root.' >&2
  exit 1
fi
deployment_user="$(.venv/bin/python -c 'from webapp.deployment_identity import service_user; print(service_user())')"
deployment_group="$(.venv/bin/python -c 'from webapp.deployment_identity import runtime_group; print(runtime_group())')"
# Read-only runtime group: never grant it access to data, .env or credentials.
getent group "$deployment_group" >/dev/null || groupadd --system "$deployment_group"
install -d -m 0700 "${VIDEO_USE_EXECUTOR_ROOT:-/var/lib/video-use-executor}"
# Frozen releases link .venv to the shared dependency directory. Recursive ACL
# traversal must start at its resolved directory, not at the symlink.
runtime_venv="$(realpath .venv)"
setfacl -R -m u:"$deployment_user":rX,g:"$deployment_group":rX helpers references
.venv/bin/python -m deploy.runtime_access
# runtime_access grants traversal to every venv parent. Apply package-directory
# listing permission afterwards so that parent traversal cannot replace it.
setfacl -m u:"$deployment_user":rx . webapp helpers
runuser -u "$deployment_user" -- "$runtime_venv/bin/python" -c 'import fastapi, psycopg, redis; assert callable(redis.Redis)'
setfacl -m g:"$deployment_group":r SKILL.md webapp/*.py
setfacl -m u:"$deployment_user":r webapp/*.py
# Every public static dependency must be readable by the unprivileged API.
setfacl -R -m u:"$deployment_user":rX webapp/static
setfacl -m g:"$deployment_group":rx webapp
setfacl -R -m g:"$deployment_group":rX runtime/public
chmod -R a+rX runtime/public
runtime_prefix="$(.venv/bin/python -c 'import sys; print(sys.base_prefix)')"
setfacl -R -m g:"$deployment_group":rX "$runtime_prefix/bin" "$runtime_prefix/lib"
if [ "${1:-}" = '--models' ]; then
  .venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
from webapp.image_sandbox import MODEL_PATHS
from webapp.deployment_identity import runtime_group
for path in MODEL_PATHS:
    if Path(path).exists():
        subprocess.run(['setfacl','-R','-m','g:'+runtime_group()+':rX',path],check=True)
PY
fi
.venv/bin/python -m webapp.runtime_image
runuser -u "$deployment_user" -- env PYTHONPATH="$PWD" "$runtime_venv/bin/python" -c 'import webapp.gateway_service, webapp.workflow_executor'
.venv/bin/python - <<'PY'
import json
import subprocess
from pathlib import Path
from webapp.deployment_identity import runtime_group
manifest=json.loads(Path('runtime/build/manifest.json').read_text())
files=sorted({manifest['python'],manifest['codex'],*manifest['tools'].values(),*manifest['libraries'].values()})
if manifest.get('code_host'):files.append(manifest['code_host'])
# Bind mounts retain inode permissions after the task drops root privileges.
subprocess.run(['setfacl','-m','g:'+runtime_group()+':rX',*files],check=True)
PY
echo 'Executor identity registry and read-only runtime permissions are ready.'
