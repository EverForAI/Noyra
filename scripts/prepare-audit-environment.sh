#!/usr/bin/env bash
set -euo pipefail
umask 0077
NOYRA_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$NOYRA_PROJECT_ROOT"
audit_venv="$NOYRA_PROJECT_ROOT/.audit-venv"
[[ ! -e "$audit_venv" && ! -L "$audit_venv" ]] || {
  echo 'The isolated .audit-venv already exists; use it or inspect it before replacing it.' >&2
  exit 2
}
bootstrap_python="${NOYRA_AUDIT_BOOTSTRAP_PYTHON:-python3}"
"$bootstrap_python" -m venv "$audit_venv"
audit_python="$audit_venv/bin/python"
if [[ ! -x "$audit_python" ]]; then audit_python="$audit_venv/Scripts/python.exe"; fi
"$audit_python" -m pip install --require-hashes --requirement requirements-dev.lock
"$audit_python" -m pip install --no-deps --no-build-isolation .
"$audit_python" -m pip check
printf 'Audit environment ready: %s\n' "$audit_python"
