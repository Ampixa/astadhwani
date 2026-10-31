#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
RES="$PWD/builds/mac/artifacts/Astadhwani.app/Contents/Resources"
if [ ! -x "$RES/python/bin/python3.11" ]; then
  echo 'Build the Mac bundle first, or run backend/server.py with your configured Python environment.' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$RES/site-packages"
exec "$RES/python/bin/python3.11" backend/server.py --model "$RES/models/wave1" --port "${ASTADHWANI_PORT:-5189}"
