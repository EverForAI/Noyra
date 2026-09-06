#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running remote model-gateway audit on $(uname -s)..."
"$python" -m pip check
"$python" -m pytest tests/test_model_gateway.py -q
"$python" -m pytest --cov=noyra.model --cov-report=term-missing --cov-fail-under=85
"$python" -m ruff check src/noyra/model src/noyra/core/database.py tests/test_model_gateway.py
"$python" -m ruff format --check src/noyra/model src/noyra/core/database.py tests/test_model_gateway.py
"$python" -m mypy src tests

echo 'Remote model-gateway audit passed.'
