#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running interaction and privacy projection audit on $(uname -s)..."
"$python" -m pytest tests/test_interaction.py -q
"$python" -m pytest --cov=noyra.interaction --cov-report=term-missing --cov-fail-under=85 tests/test_interaction.py
"$python" -m ruff check src/noyra/interaction src/noyra/core/database.py tests/test_interaction.py
"$python" -m ruff format --check src/noyra/interaction src/noyra/core/database.py tests/test_interaction.py
"$python" -m mypy src tests

echo 'Interaction and privacy projection audit passed.'
