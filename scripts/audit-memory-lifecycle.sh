#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running long-term memory lifecycle audit on $(uname -s)..."
"$python" -m pytest tests/test_memory_lifecycle.py tests/test_mind.py tests/test_sleep.py tests/test_cognition.py tests/test_goal_governance.py -q
"$python" -m pytest --cov=noyra.mind.memory --cov=noyra.mind.consolidation --cov-report=term-missing --cov-fail-under=82 tests/test_memory_lifecycle.py tests/test_mind.py
"$python" -m ruff check src tests
"$python" -m ruff format --check src tests
"$python" -m mypy src tests

echo 'Long-term memory lifecycle audit passed.'
