#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -m ruff check src tests
"$PYTHON" -m ruff format --check src tests
"$PYTHON" -m mypy
"$PYTHON" -m pytest -q
"$PYTHON" -m compileall -q src
