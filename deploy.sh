#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "observe" ]]; then
  shift
  exec .venv/bin/python deploy/observe.py "$@"
fi
exec .venv/bin/python deploy/dynamic_cluster.py "${@:-status}"
