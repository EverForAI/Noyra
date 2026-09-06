#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running autonomous action deliberation audit on $(uname -s)..."
"$python" -m pytest tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py tests/test_service.py -q
"$python" -m pytest --cov=noyra.cognition.deliberation --cov-report=term-missing --cov-fail-under=80 tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
"$python" -m ruff check src/noyra/cognition src/noyra/capability/tools.py src/noyra/core/database.py tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/capability/tools.py src/noyra/core/database.py tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
"$python" -m mypy src tests

echo 'Autonomous action deliberation audit passed.'
