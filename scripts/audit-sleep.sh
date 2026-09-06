#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running fatigue and sleep audit on $(uname -s)..."
"$python" -m pytest tests/test_sleep.py -q
"$python" -m pytest --cov=noyra.sleep --cov-report=term-missing --cov-fail-under=85 tests/test_sleep.py
"$python" -m ruff check src/noyra/sleep src/noyra/core src/noyra/mind/memory.py src/noyra/mind/belief.py src/noyra/model/gateway.py tests/test_sleep.py
"$python" -m ruff format --check src/noyra/sleep src/noyra/core src/noyra/mind/memory.py src/noyra/mind/belief.py src/noyra/model/gateway.py tests/test_sleep.py
"$python" -m mypy src tests

echo 'Fatigue and sleep audit passed.'
