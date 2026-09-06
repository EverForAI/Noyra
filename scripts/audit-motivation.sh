#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"

echo "Running intrinsic motivation and mission audit on $(uname -s)..."
"$python" -m pytest --cov=noyra.cognition.motivation --cov=noyra.cognition.cycle --cov=noyra.cognition.self_model --cov=noyra.interaction.projection --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
"$python" -m mypy src tests
echo 'Intrinsic motivation and mission audit passed.'
