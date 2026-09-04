#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
# venv first: spawned targets inherit sys.executable (zstandard lives here only)
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    exec "$PROJECT_DIR/.venv/bin/python" -m src.launcher "$@"
else
    exec python3 -m src.launcher "$@"
fi
