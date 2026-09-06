#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running world-observation and genesis audit on $(uname -s)..."
"$python" -m pytest tests/test_world.py -q
"$python" -m pytest --cov=noyra.world --cov-report=term-missing --cov-fail-under=85 tests/test_world.py
"$python" -m ruff check src/noyra/world src/noyra/core/database.py src/noyra/mind/causal.py tests/test_world.py
"$python" -m ruff format --check src/noyra/world src/noyra/core/database.py src/noyra/mind/causal.py tests/test_world.py
"$python" -m mypy src tests

echo 'World-observation and genesis audit passed.'
