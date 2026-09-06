#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running equal interaction cognition audit on $(uname -s)..."
"$python" -m pytest tests/test_cognition.py tests/test_interaction.py tests/test_service.py -q
"$python" -m pytest --cov=noyra.cognition --cov=noyra.interaction --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff check src/noyra/cognition src/noyra/interaction src/noyra/service.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/interaction src/noyra/service.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m mypy src tests

echo 'Equal interaction cognition audit passed.'
