#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/opt/video-use/skills/image-to-editable-ppt/cli${PYTHONPATH:+:$PYTHONPATH}"
exec /opt/venv/bin/python /opt/video-use/skills/image-to-editable-ppt/cli/editppt/cli.py "$@"
