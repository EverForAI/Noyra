#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

python="${NOYRA_PYTHON:-$NOYRA_PROJECT_ROOT/.venv/bin/python}"

echo "Running Ubuntu service and deployment audit on $(uname -s)..."
"$python" -m pytest tests/test_service.py tests/test_interaction.py tests/test_capability.py tests/test_m42_p1_01_sleep_deadlock.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py -q
"$python" -m pytest --cov=noyra.service --cov=noyra.interaction.projection --cov=noyra.autonomy --cov-report=term-missing --cov-fail-under=85 tests/test_service.py tests/test_interaction.py tests/test_capability.py tests/test_m42_p1_01_sleep_deadlock.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
"$python" -m ruff check src/noyra/service.py src/noyra/__main__.py src/noyra/core/at_rest.py src/noyra/core/integrity.py src/noyra/core/operator_controls.py src/noyra/autonomy src/noyra/interaction/projection.py tests/test_service.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
"$python" -m ruff format --check src/noyra/service.py src/noyra/__main__.py src/noyra/core/at_rest.py src/noyra/core/integrity.py src/noyra/core/operator_controls.py src/noyra/autonomy src/noyra/interaction/projection.py tests/test_service.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
"$python" -m mypy src tests

grep -q '127.0.0.1:8765:8765' docker-compose.yml
grep -q 'read_only: true' docker-compose.yml
grep -q 'NoNewPrivileges=true' deploy/systemd/noyra.service
grep -q 'ReadWritePaths=/var/lib/noyra' deploy/systemd/noyra.service
grep -q 'ReadOnlyPaths=/etc/noyra/backup-keyring.json' deploy/systemd/noyra.service
grep -q 'UMask=0077' deploy/systemd/noyra.service
grep -q 'NOYRA_INSTALL_PROFILE=base' Dockerfile
grep -q 'requirements-cloud.lock' Dockerfile
grep -q 'base|cloud' scripts/install-ubuntu.sh
grep -q 'requirements-cloud.lock' scripts/install-ubuntu.sh
grep -q 'backup-key init' scripts/install-ubuntu.sh
grep -q 'chmod 0640' scripts/install-ubuntu.sh
grep -q 'NOYRA_AT_REST_MODE=required' deploy/noyra.env.example
grep -q 'NOYRA_BACKUP_KEYRING_PATH=' deploy/noyra.env.example
grep -q 'NOYRA_VOLUME_ENCRYPTION_BACKEND: attestation' docker-compose.yml
grep -q 'volume-attestation.json:ro' docker-compose.yml
test -f requirements-cloud.lock
bash -n scripts/install-ubuntu.sh scripts/audit-deployment.sh

temporary_env=false
cleanup() {
  if [[ "$temporary_env" == true ]]; then
    rm -f -- .env
  fi
}
trap cleanup EXIT

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if [[ ! -f .env ]]; then
    cp -- .env.example .env
    temporary_env=true
  fi
  docker compose --project-name noyra config --quiet
fi
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify deploy/systemd/noyra.service
fi

echo 'Ubuntu service and deployment audit passed.'
