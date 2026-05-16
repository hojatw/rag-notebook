#!/usr/bin/env bash
# Bootstrap a fresh local development environment.
#
# Handles the chromadb 1.5.9 / Python 3.14 caveat: when no `onnxruntime`
# wheel is published for the active Python version, install chromadb
# without its embedding-function dep, then add the runtime extras manually.
#
# Usage:
#   ./setup.sh           # creates .venv, installs deps
#   ./setup.sh --force   # remove existing .venv first
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

if [[ "${1:-}" == "--force" ]] && [[ -d .venv ]]; then
  echo "Removing existing .venv..."
  rm -rf .venv
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv with $(python3 --version)..."
  python3 -m venv .venv
fi

VENV_PY=".venv/bin/python"

echo "Upgrading pip..."
"$VENV_PY" -m pip install --upgrade pip --quiet

# Detect whether onnxruntime has a wheel for the active interpreter; if not,
# fall back to the chromadb --no-deps workaround.
PY_TAG="$("$VENV_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "Python tag: $PY_TAG"

if "$VENV_PY" -m pip install --dry-run onnxruntime 2>&1 | grep -q "Would install"; then
  echo "onnxruntime wheel available — installing requirements normally..."
  "$VENV_PY" -m pip install -r requirements.txt
  "$VENV_PY" -m pip install chromadb==1.5.9
else
  echo "onnxruntime not available for Python $PY_TAG — using --no-deps fallback for chromadb..."
  "$VENV_PY" -m pip install -r requirements.txt
  "$VENV_PY" -m pip install chromadb==1.5.9 --no-deps
  "$VENV_PY" -m pip install \
    numpy==2.4.4 pydantic-settings==2.14.1 pybase64==1.4.3 \
    overrides jsonschema mmh3 orjson pypika tenacity typer tqdm rich \
    importlib-resources build bcrypt grpcio tokenizers \
    opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc \
    kubernetes
fi

echo
echo "Done. Start the dev server with:"
echo "  .venv/bin/python -m uvicorn app.main:app --reload --port 8000"
echo
echo "Then open http://127.0.0.1:8000 (admin / admin123)."
