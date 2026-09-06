#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running autonomous reflective sleep audit on $(uname -s)..."
"$python" -m pytest tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py -q
"$python" -m pytest --cov=noyra.cognition.reflection --cov=noyra.sleep --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
"$python" -m ruff check src/noyra/cognition src/noyra/sleep src/noyra/service.py tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/sleep src/noyra/service.py tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
"$python" -m mypy src tests

echo 'Autonomous reflective sleep audit passed.'
