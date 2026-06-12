#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Error: Python not found. Install Python 3.9+ and try again." >&2
  exit 1
fi

echo "Using $($PY --version)"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment in ./venv ..."
  "$PY" -m venv venv
else
  echo "Reusing existing ./venv"
fi

source venv/bin/activate

echo "Upgrading pip ..."
python -m pip install --upgrade pip >/dev/null

echo "Installing dependencies (this may take a minute) ..."
python -m pip install -r requirements.txt

echo
echo "Done. To start the app:"
echo "    source venv/bin/activate"
echo "    python app.py"
echo "Then open http://localhost:5000"
echo
echo "Note: the first time you use the mic, the speech model (~145MB) downloads once."

if [ "${1:-}" = "--run" ]; then
  echo
  echo "Starting the app on http://localhost:5000  (Ctrl+C to stop) ..."
  exec python app.py
fi
