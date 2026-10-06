#!/usr/bin/env bash
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
helper="$root/scripts/lib/upgrade-components.sh"
[[ -f "$helper" ]] || fail 'upgrade component transaction helper is missing'
source "$helper"

test_root="$(mktemp -d "${TMPDIR:-/tmp}/noyra-upgrade-components.XXXXXX")"
trap 'rm -rf --one-file-system -- "$test_root"' EXIT

runner="$test_root/libexec/noyra-upgrade-runner.sh"
path_unit="$test_root/systemd/noyra-upgrade.path"
service_unit="$test_root/systemd/noyra-upgrade.service"
recover_unit="$test_root/systemd/noyra-upgrade-recover.service"
main_unit="$test_root/systemd/noyra.service"
backup="$test_root/backup"
calls="$test_root/systemctl.log"
mkdir -p "$(dirname "$runner")" "$(dirname "$path_unit")"

path_enabled=enabled
path_active=active
recover_enabled=enabled
path_unit_loaded=true
systemctl() {
  printf '%s\n' "$*" >> "$calls"
  case "$1 ${2:-}" in
    'is-enabled noyra-upgrade.path') printf '%s\n' "$path_enabled" ;;
    'is-active noyra-upgrade.path')
      printf '%s\n' "$path_active"
      [[ "$path_unit_loaded" == true ]] || return 4
      ;;
    'is-enabled noyra-upgrade-recover.service') printf '%s\n' "$recover_enabled" ;;
    'stop noyra-upgrade.path')
      [[ "$path_unit_loaded" == true ]] || return 5
      path_active=inactive
      ;;
    'enable --now') path_enabled=enabled; path_active=active ;;
    'enable noyra-upgrade.path') path_enabled=enabled ;;
    'enable noyra-upgrade-recover.service') recover_enabled=enabled ;;
    'disable --now') path_enabled=disabled; path_active=inactive ;;
    'disable noyra-upgrade.path') path_enabled=disabled ;;
    'disable noyra-upgrade-recover.service') recover_enabled=disabled ;;
    daemon-reload*) ;;
    *) fail "unexpected systemctl invocation: $*" ;;
  esac
}

printf 'old runner\n' > "$runner"
printf 'old path\n' > "$path_unit"
printf 'old recovery\n' > "$recover_unit"
printf 'old main\n' > "$main_unit"
noyra_upgrade_components_snapshot \
  "$backup" "$runner" "$path_unit" "$service_unit" "$recover_unit" "$main_unit"
noyra_upgrade_components_mark_changed
printf 'new runner\n' > "$runner"
printf 'new path\n' > "$path_unit"
printf 'new service\n' > "$service_unit"
printf 'new recovery\n' > "$recover_unit"
printf 'new main\n' > "$main_unit"
noyra_upgrade_components_restore \
  "$runner" "$path_unit" "$service_unit" "$recover_unit" "$main_unit" || fail 'component restore failed'

[[ "$(cat "$runner")" == 'old runner' ]] || fail 'runner was not restored'
[[ "$(cat "$path_unit")" == 'old path' ]] || fail 'path unit was not restored'
[[ ! -e "$service_unit" ]] || fail 'new service unit remained after rollback'
[[ "$(cat "$recover_unit")" == 'old recovery' ]] || fail 'recovery unit was not restored'
[[ "$(cat "$main_unit")" == 'old main' ]] || fail 'main unit was not restored'
[[ "$path_enabled:$path_active:$recover_enabled" == 'enabled:active:enabled' ]] || \
  fail 'systemd activation state was not restored'

rm -f -- "$runner" "$path_unit" "$service_unit" "$recover_unit"
backup="$test_root/backup-absent"
path_enabled=disabled
path_active=inactive
recover_enabled=disabled
path_unit_loaded=false
noyra_upgrade_components_snapshot \
  "$backup" "$runner" "$path_unit" "$service_unit" "$recover_unit"
noyra_upgrade_components_mark_changed
printf 'new runner\n' > "$runner"
noyra_upgrade_components_restore \
  "$runner" "$path_unit" "$service_unit" "$recover_unit" || \
  fail 'restore of previously absent files failed'
[[ ! -e "$runner" && ! -e "$path_unit" && ! -e "$service_unit" && ! -e "$recover_unit" ]] || \
  fail 'files absent before install were not removed after rollback'
[[ "$path_enabled:$path_active:$recover_enabled" == 'disabled:inactive:disabled' ]] || \
  fail 'previously disabled units were not left disabled'

echo 'upgrade component rollback checks passed'
