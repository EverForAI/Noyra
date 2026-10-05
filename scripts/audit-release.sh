#!/usr/bin/env bash
set -euo pipefail
NOYRA_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$NOYRA_PROJECT_ROOT"
source "$NOYRA_PROJECT_ROOT/scripts/lib/audit-environment.sh"
PYTHON="$(audit_python)"
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON" "$NOYRA_PROJECT_ROOT/scripts/verify-committed.py" --commit HEAD --scope all
