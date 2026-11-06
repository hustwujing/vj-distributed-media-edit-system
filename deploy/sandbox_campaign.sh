#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

export DAS_DEPLOY_NAMESPACE=sandbox-agent
export DAS_DEPLOY_ROOT="$repo/.sandbox-cluster"
export DAS_DEPLOY_CONF=/etc/distributed-agent-sandbox-backend
export DAS_DEPLOY_EXECUTORS=/data2/wujing03/das-sandbox-exec
export DAS_DEPLOY_API_A_PORT=20766
export DAS_DEPLOY_API_B_PORT=20767
export DAS_DEPLOY_OBS_PORT=20768
export DAS_DEPLOY_PROXY_PORT=20765
export DAS_DEPLOY_UID_A=720000
export DAS_DEPLOY_UID_B=720100
export DAS_DEPLOY_ENTRY=/distributed-agent-sandbox
export DAS_DEPLOY_DATABASE=distributed_agent_sandbox
export DAS_DEPLOY_ROLE=distributed_agent_sandbox
export DAS_DEPLOY_ORCHESTRATOR_ROLE=distributed_agent_sandbox_orchestrator
export DAS_DEPLOY_OBJECT_PREFIX=distributed-agent-sandbox
export DAS_DEPLOY_STREAM_PREFIX=distributed-agent-sandbox:events:
export DAS_DEPLOY_GPU_LEASE_ROOT=/data2/wujing03/das-sandbox-gpu-leases
export DAS_DEPLOY_MEDIA_RUNTIME_PREFIX=das-sandbox-media-
export DAS_DEPLOY_CONTROL_LOCK=/run/distributed-agent-sandbox-control.lock

export DYNAMIC_CASE_DEPLOY_ROOT="$repo/.sandbox-cluster"
export DYNAMIC_CASE_BASE=http://127.0.0.1/distributed-agent-sandbox
export DYNAMIC_EXPECT_PATH=/distributed-agent-sandbox
export DYNAMIC_EXPECT_NAMESPACE=sandbox-agent
export DYNAMIC_EXPECT_OBJECT_PREFIX=distributed-agent-sandbox
export DYNAMIC_EXPECT_STREAM_PREFIX=distributed-agent-sandbox:

exec "$repo/.venv/bin/python" "$repo/tests/campaign_suite.py" "$@" --base "$DYNAMIC_CASE_BASE"
