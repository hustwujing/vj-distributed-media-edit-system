#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installation step as root.' >&2
  exit 1
fi
# Read-only runtime group: never grant it access to data, .env or credentials.
getent group video-use-runtime >/dev/null || groupadd --system video-use-runtime
install -d -m 0700 /var/lib/video-use-executor
# Python imports list package directories. The broker needs the repository
# directory readable; private files still retain their own restrictive modes.
setfacl -m u:video-use:rx . webapp helpers
# Frozen releases link .venv to the shared dependency directory. Recursive ACL
# traversal must start at its resolved directory, not at the symlink.
runtime_venv="$(realpath .venv)"
setfacl -R -m g:video-use-runtime:rX helpers references
.venv/bin/python -m deploy.runtime_access
runuser -u video-use -- "$runtime_venv/bin/python" -c 'import fastapi, psycopg, redis; assert callable(redis.Redis)'
setfacl -m g:video-use-runtime:r SKILL.md webapp/*.py
setfacl -m u:video-use:r webapp/*.py
# Every public static dependency must be readable by the unprivileged API.
setfacl -R -m u:video-use:rX webapp/static
setfacl -m g:video-use-runtime:rx webapp
setfacl -R -m g:video-use-runtime:rX runtime/public
chmod -R a+rX runtime/public
runtime_prefix="$(.venv/bin/python -c 'import sys; print(sys.base_prefix)')"
setfacl -R -m g:video-use-runtime:rX "$runtime_prefix/bin" "$runtime_prefix/lib"
if [ "${1:-}" = '--models' ]; then
  .venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
from webapp.image_sandbox import MODEL_PATHS
for path in MODEL_PATHS:
    if Path(path).exists():
        subprocess.run(['setfacl','-R','-m','g:video-use-runtime:rX',path],check=True)
PY
fi
.venv/bin/python -m webapp.runtime_image
.venv/bin/python - <<'PY'
import json
import subprocess
from pathlib import Path
manifest=json.loads(Path('runtime/build/manifest.json').read_text())
files=sorted({manifest['python'],manifest['codex'],*manifest['tools'].values(),*manifest['libraries'].values()})
if manifest.get('code_host'):files.append(manifest['code_host'])
# Bind mounts retain inode permissions after the task drops root privileges.
subprocess.run(['setfacl','-m','g:video-use-runtime:rX',*files],check=True)
PY
echo 'Executor identity registry and read-only runtime permissions are ready.'
