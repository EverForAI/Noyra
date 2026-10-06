#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/../../scripts/lib/control-layout.sh"
root="$(mktemp -d)"
trap 'rm -rf -- "$root"' EXIT

mkdir "$root/existing"
chmod 0700 "$root/existing"
noyra_control_layout_snapshot "$root" "$root/existing" "$root/upgrade" \
  "$root/upgrade/requests" "$root/upgrade/processing" "$root/upgrade/manager.lock"
chmod 0750 "$root/existing"
mkdir -p "$root/upgrade/requests" "$root/upgrade/processing"
touch "$root/upgrade/manager.lock"
noyra_control_layout_record_file "$root/upgrade/manager.lock"
noyra_control_layout_restore
[[ "$(stat -c '%a' "$root/existing")" == 700 && ! -e "$root/upgrade" ]]

# Runtime-created records must survive; rollback must report that restart is unsafe.
noyra_control_layout_snapshot "$root" "$root/upgrade" "$root/upgrade/manager.lock"
mkdir "$root/upgrade"
touch "$root/upgrade/manager.lock"
noyra_control_layout_record_file "$root/upgrade/manager.lock"
printf 'active-fence' > "$root/upgrade/runtime-record"
if noyra_control_layout_restore; then
  echo 'Rollback discarded unexpected runtime state' >&2
  exit 1
fi
[[ "$(<"$root/upgrade/runtime-record")" == active-fence ]]
echo 'control layout rollback checks passed'
