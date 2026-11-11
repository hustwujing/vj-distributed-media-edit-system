#!/usr/bin/env bash
set -euo pipefail

export XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-${HOME:-/workspace/state/home}/.config}
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/workspace/tmp/cache}
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
exec xvfb-run -a /usr/bin/drawio --no-sandbox "$@"
