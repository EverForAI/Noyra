#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT_DIR/scripts/noyra-migration-runner.sh"
AGENT="$ROOT_DIR/scripts/noyra-migration-agent.py"

test -f "$RUNNER"
test -f "$AGENT"
bash -n "$RUNNER"
grep -q '^set -euo pipefail$' "$RUNNER"
grep -q 'No arbitrary command execution' "$RUNNER"
grep -q 'REQUEST_ID' "$RUNNER"
! grep -Eq '(^|[[:space:]])(eval|ssh|scp)[[:space:]]' "$RUNNER"
grep -q 'NoNewPrivileges=true' "$ROOT_DIR/deploy/systemd/noyra-migration-agent.service"
grep -q 'NoNewPrivileges=true' "$ROOT_DIR/deploy/systemd/noyra-migration-runner.service"
grep -q 'ReadWritePaths=/var/lib/noyra/migration' "$ROOT_DIR/deploy/systemd/noyra-migration-agent.service"
grep -q 'ReadWritePaths=/var/lib/noyra/migration' "$ROOT_DIR/deploy/systemd/noyra-migration-runner.service"

if "$RUNNER" --request-id '../escape' status >/dev/null 2>&1; then
  echo 'runner accepted traversal request id' >&2
  exit 1
fi
if "$RUNNER" --request-id 'bad id' status >/dev/null 2>&1; then
  echo 'runner accepted unsafe request id' >&2
  exit 1
fi
if "$RUNNER" --request-id request-1 unknown >/dev/null 2>&1; then
  echo 'runner accepted unknown action' >&2
  exit 1
fi

echo 'migration runner shell contract passed'
