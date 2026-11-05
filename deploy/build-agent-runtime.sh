#!/usr/bin/env bash
set -euo pipefail

docker_bin="${DOCKER:-docker}"

require_digest() {
  local name="$1" value="$2"
  case "$value" in
    sha256:????????????????????????????????????????????????????????????????|*@sha256:????????????????????????????????????????????????????????????????) ;;
    *) echo "$name must be sha256:<local-image-id> or image@sha256:<manifest>" >&2; exit 2;;
  esac
}

case "${1:-}" in
  foundation)
    require_digest PYTHON_IMAGE "${PYTHON_IMAGE:-}"
    "$docker_bin" build -f runtime/opensandbox/foundation.Dockerfile \
      --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE" \
      --build-arg "DEBIAN_MIRROR=${DEBIAN_MIRROR:-http://deb.debian.org}" \
      -t opensandbox-runtime-foundation:build .
    ;;
  base)
    test -d wheels
    require_digest BASE_IMAGE "${BASE_IMAGE:-}"
    "$docker_bin" build --network=none -f runtime/opensandbox/base.Dockerfile \
      --build-arg "BASE_IMAGE=$BASE_IMAGE" -t agent-runtime-base:build .
    ;;
  codex|claude|deepseek)
    harness="$1"
    test "$harness" != codex || test -x vendor/codex
    test "$harness" = codex || test -d wheels
    require_digest RUNTIME_BASE "${RUNTIME_BASE:-}"
    "$docker_bin" build --network=none -f "runtime/opensandbox/$harness.Dockerfile" \
      --build-arg "RUNTIME_BASE=$RUNTIME_BASE" -t "agent-runtime-$harness:build" .
    ;;
  harnesses)
    test -d wheels
    require_digest RUNTIME_BASE "${RUNTIME_BASE:-}"
    test -x vendor/codex
    for harness in codex claude deepseek; do
      "$docker_bin" build --network=none -f "runtime/opensandbox/$harness.Dockerfile" \
        --build-arg "RUNTIME_BASE=$RUNTIME_BASE" -t "agent-runtime-$harness:build" .
    done
    ;;
  *) echo 'usage: build-agent-runtime.sh foundation|base|codex|claude|deepseek|harnesses' >&2; exit 2;;
esac
