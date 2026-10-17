#!/usr/bin/env bash
# Start the LOCAL DEMO: the local customer API (background) and the Streamlit UI with the Local Demo Lab enabled.
# Ctrl+C stops both. Refuses to start if a port is already in use.
set -euo pipefail
cd "$(dirname "$0")/.."
API_PORT="${LOCAL_SERVICE_PORT:-8766}"
UI_PORT="${STREAMLIT_PORT:-8501}"
export ADAPTIX_LOCAL_DEMO=1
export ADAPTIX_LOCAL_SERVICE_URL="http://127.0.0.1:${API_PORT}"
busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
for p in "$API_PORT" "$UI_PORT"; do
  if busy "$p"; then
    echo "Port $p is already in use:"; lsof -nP -iTCP:"$p" -sTCP:LISTEN | tail -n +2
    echo "Stop that process, or choose other ports: LOCAL_SERVICE_PORT=8770 STREAMLIT_PORT=8502 $0"
    exit 1
  fi
done
[ -x .venv/bin/python ] || { echo "Run scripts/setup_local.sh first."; exit 1; }
.venv/bin/python -m adaptix.localdemo db init >/dev/null
.venv/bin/python -m adaptix.localdemo service --port "$API_PORT" &
SVC=$!
trap 'kill $SVC 2>/dev/null || true' EXIT INT TERM
sleep 1
kill -0 $SVC 2>/dev/null || { echo "The local customer API did not start."; exit 1; }
.venv/bin/python -m adaptix.localdemo check || echo "(Not fully ready yet - the Local Demo Lab shows what is missing.)"
.venv/bin/streamlit run app.py --server.port "$UI_PORT" --server.address 127.0.0.1 --server.headless true
