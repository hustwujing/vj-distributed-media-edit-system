#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
set -a
source "$repo/.distributed.env"
source "$repo/.storage.env"
source /etc/distributed-agent-sandbox-backend/node-a.env
set +a
export DYNAMIC_CASE_DEPLOY_ROOT="$repo/.sandbox-cluster"
export VIDEO_USE_DEPLOYMENT=sandbox-agent
exec "$repo/.venv/bin/python" "$repo/tests/run_opensandbox_recovery_live.py"
