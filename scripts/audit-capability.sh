#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running capability and autonomy-loop audit on $(uname -s)..."
"$python" -m pytest tests/test_capability.py -q
"$python" -m pytest --cov=noyra.capability --cov=noyra.autonomy --cov-report=term-missing --cov-fail-under=85 tests/test_capability.py
"$python" -m ruff check src/noyra/capability src/noyra/autonomy src/noyra/core/database.py tests/test_capability.py
"$python" -m ruff format --check src/noyra/capability src/noyra/autonomy src/noyra/core/database.py tests/test_capability.py
"$python" -m mypy src tests

echo 'Capability and autonomy-loop audit passed.'
