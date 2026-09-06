#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"
database="$NOYRA_DATA_DIR/audit-kernel.sqlite3"

cleanup() {
  rm -f -- "$database" "$database-wal" "$database-shm" "$database.lock"
}
trap cleanup EXIT
cleanup

echo "Running deterministic subject-kernel audit on $(uname -s)..."
"$python" -m pytest tests/test_kernel.py -q
"$python" -m pytest --cov=noyra.core --cov-report=term-missing --cov-fail-under=85
"$python" -m ruff check src/noyra/core tests/test_kernel.py
"$python" -m ruff format --check src/noyra/core tests/test_kernel.py
"$python" -m mypy src tests
"$python" -c "from pathlib import Path; from noyra.core import SubjectKernel; from noyra.core.types import content_hash; p=Path(r'$database'); k=SubjectKernel(p, 'Noyra-audit', content_hash({'seed':'audit'})); k.boot(); k.activate(); k.checkpoint({'audit':'ok'}, reason='kernel audit'); print(k.health())"
"$python" -c "import sqlite3; c=sqlite3.connect(r'$database'); assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'; assert c.execute('PRAGMA foreign_key_check').fetchall() == []; print('SQLite integrity checks passed')"

echo 'Subject-kernel audit passed.'
