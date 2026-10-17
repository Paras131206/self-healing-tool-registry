#!/usr/bin/env bash
# Install/check the Python dependencies for the LOCAL DEMO (creates .venv if missing). Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"' \
  || { echo "Python 3.11 or newer is required (https://www.python.org/downloads/)."; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt -r requirements-dev.txt
echo "Python dependencies installed in .venv"
.venv/bin/python -m adaptix.localdemo db init
