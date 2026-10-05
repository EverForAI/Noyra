#!/usr/bin/env bash
set -euo pipefail
NOYRA_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$NOYRA_PROJECT_ROOT"
source "$NOYRA_PROJECT_ROOT/scripts/lib/audit-environment.sh"
PYTHON="$(audit_python)"
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON" -m ruff check src tests
"$PYTHON" -m ruff format --check src tests
"$PYTHON" -m mypy
"$PYTHON" -m pytest -q
"$PYTHON" -m compileall -q src
