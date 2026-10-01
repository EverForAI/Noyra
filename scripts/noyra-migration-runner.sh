#!/usr/bin/env bash
set -euo pipefail

# Root-owned runner boundary. No arbitrary command execution: the request id is
# only a safe filename segment and every action maps to a fixed operation.
DATA_ROOT="/var/lib/noyra/migration"
REQUEST_DIR="$DATA_ROOT/requests"
STATUS_DIR="$DATA_ROOT/status"
REQUEST_ID=""
ACTION=""

usage() {
  echo "usage: $0 --request-id SAFE_ID {status|restore|health|fence}" >&2
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
    status|restore|health|fence)
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

case "$ACTION" in
  status)
    status_file="$STATUS_DIR/$REQUEST_ID.json"
    [[ -f "$status_file" && ! -L "$status_file" ]] || {
      echo '{"status":"not_found"}'
      exit 0
    }
    cat -- "$status_file"
    ;;
  restore)
    request_file="$REQUEST_DIR/$REQUEST_ID.json"
    [[ -f "$request_file" && ! -L "$request_file" ]] || {
      echo 'migration restore request is unavailable' >&2
      exit 1
    }
    echo 'migration restore requires a verified target-agent receipt' >&2
    exit 78
    ;;
  health|fence)
    echo "migration $ACTION requires an authenticated agent receipt" >&2
    exit 78
    ;;
esac
