#!/usr/bin/env sh
# One-step launcher for macOS and Linux: creates a private Python environment
# in .venv the first time, keeps its packages up to date, and opens the web app.
#   ./scripts/start.sh              start on http://127.0.0.1:8420
#   ./scripts/start.sh --port 9000  any `lucidfish web` option works
set -e
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/ (or your package manager)."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating a Python environment in .venv (this takes a minute)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
fi
.venv/bin/python -m pip install --quiet -e .
exec .venv/bin/python -m lucidfish web "$@"
