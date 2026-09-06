#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running outcome evaluation and strategy learning audit on $(uname -s)..."
"$python" -m pytest tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py -q
"$python" -m pytest --cov=noyra.learning --cov-report=term-missing --cov-fail-under=85 tests/test_outcome_learning.py
"$python" -m ruff check src/noyra/learning src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py
"$python" -m ruff format --check src/noyra/learning src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py
"$python" -m mypy src tests

echo 'Outcome evaluation and strategy learning audit passed.'
