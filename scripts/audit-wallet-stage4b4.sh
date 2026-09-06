#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

"$ROOT/.venv/bin/python" "$SCRIPT_DIR/audit-wallet-stage4b4.py" "$@"
