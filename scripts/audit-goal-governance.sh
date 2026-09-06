#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running autonomous goal governance audit on $(uname -s)..."
"$python" -m pytest tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py -q
"$python" -m pytest --cov=noyra.cognition.governance --cov=noyra.cognition.reflection --cov=noyra.interaction.projection --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
"$python" -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
"$python" -m mypy src tests

echo 'Autonomous goal governance audit passed.'
