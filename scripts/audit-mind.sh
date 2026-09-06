#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running causal mind-state audit on $(uname -s)..."
"$python" -m pytest tests/test_mind.py -q
"$python" -m pytest --cov=noyra.mind --cov-report=term-missing --cov-fail-under=85
"$python" -m ruff check src/noyra/mind src/noyra/core/database.py tests/test_mind.py
"$python" -m ruff format --check src/noyra/mind src/noyra/core/database.py tests/test_mind.py
"$python" -m mypy src tests

echo 'Causal mind-state audit passed.'
