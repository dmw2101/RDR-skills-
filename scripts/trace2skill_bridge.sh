#!/usr/bin/env bash
# CPU-only source launcher; creates a standard-library-only venv without downloads.
set -euo pipefail
bridge_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bridge_venv="$bridge_root/.venv-trace2skill"
cd "$bridge_root"
if [[ "${1:-}" == "setup" ]]; then
    if [[ ! -x "$bridge_venv/bin/python" ]]; then
        python3.12 -m venv --copies "$bridge_venv"
    fi
    "$bridge_venv/bin/python" --version
    "$bridge_venv/bin/python" -m pip check
    printf '%s\n' "Ready: $bridge_venv (no packages downloaded; bridge uses the standard library)"
    exit 0
fi
if [[ ! -x "$bridge_venv/bin/python" ]]; then
    printf '%s\n' 'Run this launcher with setup first.' >&2
    exit 2
fi
export PYTHONPATH="$bridge_root/src"
exec "$bridge_venv/bin/python" -B -m trace2skill_bridge "$@"
