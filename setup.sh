#!/usr/bin/env bash
# Bootstrap a fresh local development environment.
#
# Uses Python 3.12 to match the Docker runtime and keep native wheels
# such as onnxruntime available on local development machines.
#
# Usage:
#   ./setup.sh           # creates .venv, installs deps
#   ./setup.sh --force   # remove existing .venv first
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3.12}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: $PYTHON_BIN was not found."
  echo "Install Python 3.12, or run with PYTHON_BIN=/path/to/python3.12 ./setup.sh"
  exit 1
fi

if [[ "${1:-}" == "--force" ]] && [[ -d .venv ]]; then
  echo "Removing existing .venv..."
  rm -rf .venv
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv with $("$PYTHON_BIN" --version)..."
  "$PYTHON_BIN" -m venv .venv
fi

VENV_PY=".venv/bin/python"

PY_TAG="$("$VENV_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "Python tag: $PY_TAG"

if [[ "$PY_TAG" != "3.12" ]]; then
  echo "Error: local development uses Python 3.12 to match Docker."
  echo "Current .venv uses Python $PY_TAG. Re-run with Python 3.12."
  exit 1
fi

echo "Upgrading pip..."
"$VENV_PY" -m pip install --upgrade pip --quiet

"$VENV_PY" -m pip install -r requirements.txt

echo
echo "Done. Start the dev server with:"
echo "  .venv/bin/python -m uvicorn app.main:app --reload --port 8000"
echo
echo "Then open http://127.0.0.1:8000 (admin / admin123)."
