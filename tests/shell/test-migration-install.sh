#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$ROOT_DIR/scripts/install-ubuntu.sh"

test -f "$INSTALLER"
bash -n "$INSTALLER"

# Migration files are published as root-owned regular files and are included
# in the same release transaction as the service. The agent is only enabled if
# the operator had already enabled it; fresh installs stay disabled.
grep -q 'noyra_migration_components_snapshot' "$INSTALLER"
grep -q 'noyra_migration_components_restore' "$INSTALLER"
grep -q 'install -o root -g root -m 0750.*noyra-migration-runner.sh' "$INSTALLER"
grep -q 'install -o root -g root -m 0644.*noyra-migration-agent.service' "$INSTALLER"
grep -q 'install -o root -g root -m 0644.*noyra-migration-runner.service' "$INSTALLER"
grep -q 'MIGRATION_AGENT_WAS_ENABLED' "$INSTALLER"
grep -q 'if \[\[ "\$MIGRATION_AGENT_WAS_ENABLED" == true \]\]' "$INSTALLER"
grep -q 'Migration system file must be regular and not a symlink' "$INSTALLER"
grep -q 'Migration identity must be a regular file and not a symlink' "$INSTALLER"
grep -q 'chown root:noyra.*identity.json' "$INSTALLER"
grep -q 'chmod 0640.*identity.json' "$INSTALLER"
grep -q 'migration/source' "$INSTALLER"
grep -q 'migration/fences' "$INSTALLER"
grep -q 'install -d -o noyra -g noyra -m 0700.*migration/source' "$INSTALLER"
grep -q 'chmod 0700.*migration/source' "$INSTALLER"
grep -q 'source_epoch_file=' "$INSTALLER"
grep -q 'runtime-.*row\[0\]' "$INSTALLER"

# The systemd units must not inherit the ordinary environment file or expose
# the loopback agent beyond the local host.
agent_unit="$ROOT_DIR/deploy/systemd/noyra-migration-agent.service"
runner_unit="$ROOT_DIR/deploy/systemd/noyra-migration-runner.service"
grep -q 'User=noyra' "$agent_unit"
grep -q 'NoNewPrivileges=true' "$agent_unit"
grep -q 'ReadOnlyPaths=/etc/noyra/migration/identity.json' "$agent_unit"
grep -q -- '--listen 127.0.0.1:8876' "$agent_unit"
! grep -Eq 'EnvironmentFile=.*noyra\.env' "$agent_unit"
grep -q 'User=root' "$runner_unit"
grep -q 'NoNewPrivileges=true' "$runner_unit"
grep -q 'ReadWritePaths=/var/lib/noyra/migration' "$runner_unit"
grep -q 'migration/source' "$runner_unit"
grep -q 'migration/fences' "$runner_unit"
grep -q 'LoadCredential=backup-keyring:/etc/noyra/backup-keyring.json' "$agent_unit"
grep -q -- '--restore-root /var/lib/noyra/migration-agent/restored' "$agent_unit"
grep -q -- '--backup-keyring %d/backup-keyring' "$agent_unit"

echo 'migration install contract passed'
