#!/usr/bin/env bash
# Starts the controller against a running simulator and returns when the scenario has finished.
#   ./run.sh http://127.0.0.1:8700
# Needs Python >= 3.10 with httpx (already in requirements.txt). No other services.
# State: state/controller.db (SQLite). Re-running against the same world resumes; against a new
# world the old store is archived automatically. Override with CONTROLLER_DB=...
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi
fi
# Pinned so any set iteration we missed cannot make two runs of the same world differ.
export PYTHONHASHSEED=0
exec "$PY" -m controller --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}" --step "${CONTROLLER_STEP:-60}"
