#!/usr/bin/env bash
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/lib/upgrade-components.sh"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/noyra-migration-components.XXXXXX")"
trap 'rm -rf --one-file-system -- "$test_root"' EXIT

runner="$test_root/libexec/noyra-migration-runner.sh"
agent_unit="$test_root/systemd/noyra-migration-agent.service"
runner_unit="$test_root/systemd/noyra-migration-runner.service"
target_path_unit="$test_root/systemd/noyra-target-activation.path"
backup="$test_root/backup"
mkdir -p "$(dirname "$runner")" "$(dirname "$agent_unit")"
agent_enabled=enabled
agent_active=active
target_path_enabled=enabled
systemctl() {
  case "$1 ${2:-}" in
    'is-enabled noyra-migration-agent.service') printf '%s\n' "$agent_enabled" ;;
    'is-active noyra-migration-agent.service') printf '%s\n' "$agent_active" ;;
    'is-enabled noyra-target-activation.path') printf '%s\n' "$target_path_enabled" ;;
    'stop noyra-migration-agent.service') agent_active=inactive ;;
    'enable noyra-migration-agent.service') agent_enabled=enabled ;;
    'enable --now')
      [[ "${3:-}" == noyra-target-activation.path ]] || fail "unexpected enabled path: ${3:-}"
      target_path_enabled=enabled
      ;;
    'start noyra-migration-agent.service') agent_active=active ;;
    'disable --now') agent_enabled=disabled; agent_active=inactive ;;
    daemon-reload*) ;;
    *) fail "unexpected systemctl invocation: $*" ;;
  esac
}

printf 'old runner\n' > "$runner"
printf 'old agent\n' > "$agent_unit"
noyra_migration_components_snapshot "$backup" "$runner" "$agent_unit" "$runner_unit" "$target_path_unit"
noyra_migration_components_mark_changed
printf 'new runner\n' > "$runner"
printf 'new agent\n' > "$agent_unit"
printf 'new runner unit\n' > "$runner_unit"
noyra_migration_components_restore "$runner" "$agent_unit" "$runner_unit" "$target_path_unit" || fail 'restore failed'
[[ "$(cat "$runner")" == 'old runner' ]] || fail 'runner was not restored'
[[ "$(cat "$agent_unit")" == 'old agent' ]] || fail 'agent unit was not restored'
[[ ! -e "$runner_unit" ]] || fail 'new runner unit remained'
[[ "$agent_enabled:$agent_active" == 'enabled:active' ]] || fail 'activation state changed'
[[ "$target_path_enabled" == enabled ]] || fail 'activation path enablement changed'

echo 'migration component rollback checks passed'
