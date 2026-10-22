#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
printf '%s\n' 'Ready. Run: ./run.sh check'
