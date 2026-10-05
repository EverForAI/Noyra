#!/usr/bin/env bash

audit_python() {
  local candidate
  if [[ -n "${NOYRA_PYTHON:-}" ]]; then
    candidate="$NOYRA_PYTHON"
  elif [[ -x "$NOYRA_PROJECT_ROOT/.audit-venv/bin/python" ]]; then
    candidate="$NOYRA_PROJECT_ROOT/.audit-venv/bin/python"
  elif [[ -x "$NOYRA_PROJECT_ROOT/.audit-venv/Scripts/python.exe" ]]; then
    candidate="$NOYRA_PROJECT_ROOT/.audit-venv/Scripts/python.exe"
  elif [[ -x "$NOYRA_PROJECT_ROOT/.venv/bin/python" ]]; then
    candidate="$NOYRA_PROJECT_ROOT/.venv/bin/python"
  else
    candidate="$NOYRA_PROJECT_ROOT/.venv/Scripts/python.exe"
  fi
  if [[ ! -x "$candidate" ]] || ! "$candidate" -c \
    'import pytest, ruff, mypy, coverage, pytest_cov' >/dev/null 2>&1; then
    echo 'Audit Python or developer tools are missing. Prepare an isolated environment:' >&2
    printf 'bash "%s/scripts/prepare-audit-environment.sh"\n' "$NOYRA_PROJECT_ROOT" >&2
    echo 'Do not install developer dependencies into /opt/noyra/current/.venv.' >&2
    return 2
  fi
  printf '%s\n' "$candidate"
}
