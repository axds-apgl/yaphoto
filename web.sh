#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
export PYTHONUTF8=1
exec .venv/bin/python web.py "$@"
