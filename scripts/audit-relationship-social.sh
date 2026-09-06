#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"

echo "Running relationship continuity and proactive social cognition audit on $(uname -s)..."
"$python" -m pytest --cov=noyra.cognition.social --cov=noyra.cognition.interaction --cov=noyra.mind.relationship --cov=noyra.interaction.integrity --cov=noyra.interaction.projection --cov-report=term-missing --cov-fail-under=82 tests/test_relationship_social.py tests/test_cognition.py tests/test_interaction.py
"$python" -m ruff check src/noyra/cognition src/noyra/mind/relationship.py src/noyra/interaction src/noyra/core/database.py tests/test_relationship_social.py tests/test_cognition.py tests/test_interaction.py
"$python" -m ruff format --check src/noyra/cognition src/noyra/mind/relationship.py src/noyra/interaction src/noyra/core/database.py tests/test_relationship_social.py tests/test_cognition.py tests/test_interaction.py
"$python" -m mypy src tests
echo 'Relationship continuity and proactive social cognition audit passed.'
