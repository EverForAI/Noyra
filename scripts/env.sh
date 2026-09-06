#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NOYRA_PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
NOYRA_ROOT="$(cd -- "$NOYRA_PROJECT_ROOT/.." && pwd)"
NOYRA_CACHE_ROOT="$NOYRA_ROOT/.cache"

export NOYRA_PROJECT_ROOT
export NOYRA_DATA_DIR="$NOYRA_PROJECT_ROOT/.runtime/data"
export NOYRA_LOG_DIR="$NOYRA_PROJECT_ROOT/.runtime/logs"
export NOYRA_ARTIFACT_DIR="$NOYRA_PROJECT_ROOT/.runtime/artifacts"
export PIP_CACHE_DIR="$NOYRA_CACHE_ROOT/pip"
export PLAYWRIGHT_BROWSERS_PATH="$NOYRA_CACHE_ROOT/playwright"
export TMPDIR="$NOYRA_CACHE_ROOT/tmp"
export PYTHONDONTWRITEBYTECODE=1

mkdir -p \
  "$NOYRA_DATA_DIR" \
  "$NOYRA_LOG_DIR" \
  "$NOYRA_ARTIFACT_DIR" \
  "$PIP_CACHE_DIR" \
  "$PLAYWRIGHT_BROWSERS_PATH" \
  "$TMPDIR"
