#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running epistemic review and runtime export audit on $(uname -s)..."
"$python" -m pytest tests/test_epistemic_review.py tests/test_service.py tests/test_world.py tests/test_cognition.py -q
"$python" -m pytest --cov=noyra.cognition.epistemic --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_epistemic_review.py tests/test_service.py
"$python" -m ruff check src tests
"$python" -m ruff format --check src tests
"$python" -m mypy src tests

echo 'Epistemic review and runtime export audit passed.'
