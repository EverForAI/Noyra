#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running autonomous research audit on $(uname -s)..."
"$python" -m pytest tests/test_research.py tests/test_cognition.py tests/test_service.py -q
"$python" -m pytest --cov=noyra.cognition.research --cov=noyra.research --cov-report=term-missing --cov-fail-under=80 tests/test_research.py tests/test_cognition.py tests/test_service.py
"$python" -m ruff check src/noyra/research src/noyra/cognition/research.py src/noyra/service.py src/noyra/core/database.py tests/test_research.py tests/test_cognition.py tests/test_service.py
"$python" -m ruff format --check src/noyra/research src/noyra/cognition/research.py src/noyra/service.py src/noyra/core/database.py tests/test_research.py tests/test_cognition.py tests/test_service.py
"$python" -m mypy src tests

echo 'Autonomous research audit passed.'
