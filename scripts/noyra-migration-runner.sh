#!/usr/bin/env bash
set -euo pipefail

# Root-owned runner boundary. No arbitrary command execution: the request id is
# only a safe filename segment and every action maps to a fixed operation.
DATA_ROOT="${NOYRA_MIGRATION_DATA_ROOT:-/var/lib/noyra/migration}"
REQUEST_DIR="$DATA_ROOT/requests"
STATUS_DIR="$DATA_ROOT/status"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REQUEST_ID=""
ACTION=""

usage() {
  echo "usage: $0 --request-id SAFE_ID {status|restore|health|fence|unfence}" >&2
}

safe_segment() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --request-id)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REQUEST_ID="$2"
      shift 2
      ;;
    status|restore|health|fence|unfence)
      [[ -z "$ACTION" ]] || { usage; exit 2; }
      ACTION="$1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

safe_segment "$REQUEST_ID" || { echo 'unsafe migration request id' >&2; exit 2; }
[[ -n "$ACTION" ]] || { usage; exit 2; }
[[ "$(id -u)" -eq 0 ]] || { echo 'migration runner must run as root' >&2; exit 1; }

# The installed root helper is independent of release imports and caller PATH.
# Neither NOYRA_PYTHON nor NOYRA_MIGRATION_RUNNER_PY can select executable code.
runner_python="$SCRIPT_DIR/noyra-migration-runner.py"
[[ -f "$runner_python" && ! -L "$runner_python" ]] || {
  echo 'installed migration runner implementation is unavailable' >&2
  exit 1
}
[[ "$(stat -c '%u' -- "$runner_python")" == 0 ]] || {
  echo 'migration runner implementation must be owned by root' >&2
  exit 1
}
helper_mode="$(stat -c '%a' -- "$runner_python")"
(( (8#$helper_mode & 8#022) == 0 )) || {
  echo 'migration runner implementation must not be group or world writable' >&2
  exit 1
}
NOYRA_MIGRATION_DATA_ROOT="$DATA_ROOT" exec /usr/bin/python3 -I "$runner_python" --request-id "$REQUEST_ID" "$ACTION"
