#!/usr/bin/env bash
# Runs one scenario end to end: fresh simulator -> ./run.sh -> grade against a baseline.
#   scripts/run_scenario.sh public_02_incidents [port]
# Writes runs/mine_<name>/, runs/base_<name>/ (if missing), reports/<name>.md, state/<name>.db
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${1:?scenario name, e.g. public_01_dev}"
PORT="${2:-8711}"
PY="${PYTHON:-.venv/bin/python}"
SC="scenarios/$NAME.yaml"
mkdir -p reports state logs
[[ -f "runs/base_$NAME/_truth/aggregates.json" ]] || "$PY" -m sim baseline --scenario "$SC" --run-dir "runs/base_$NAME"
"$PY" -m sim serve --scenario "$SC" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill "$SIM_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 240); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  kill -0 "$SIM_PID" 2>/dev/null || { echo "simulator died; see logs/sim_$NAME.log"; exit 1; }
  sleep 0.5
done
rm -f "state/$NAME.db" "state/$NAME.db-wal" "state/$NAME.db-shm"
START=$(date +%s)
CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT" 2>&1 | tee "logs/controller_$NAME.log"
echo "controller wall-clock: $(( $(date +%s) - START ))s" | tee -a "logs/controller_$NAME.log"
"$PY" -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md" > /dev/null
echo "report: reports/$NAME.md"
