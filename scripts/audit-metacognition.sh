#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"

echo "Running metacognitive control audit on $(uname -s)..."
"$python" -m pytest --cov=noyra.cognition.metacognition --cov=noyra.cognition.cycle --cov=noyra.interaction.projection --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_metacognitive_control.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_metacognitive_control.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_metacognitive_control.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
"$python" -m mypy src tests
echo 'Metacognitive control audit passed.'
