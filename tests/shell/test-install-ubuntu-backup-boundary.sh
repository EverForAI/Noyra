#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail 'run as root'
command -v runuser >/dev/null 2>&1 || fail 'runuser is required'
command -v flock >/dev/null 2>&1 || fail 'flock is required'

test_root="$(mktemp -d /tmp/noyra-installer-boundary.XXXXXX)"
chmod 0755 "$test_root"
service_user="noyra-f02-${RANDOM}-$$"
lock_pid=""
cleanup() {
  if [[ -n "$lock_pid" ]]; then
    kill "$lock_pid" >/dev/null 2>&1 || true
    wait "$lock_pid" >/dev/null 2>&1 || true
  fi
  rm -rf --one-file-system -- "$test_root"
  userdel "$service_user" >/dev/null 2>&1 || true
}
trap cleanup EXIT

useradd --system --no-create-home --shell /usr/sbin/nologin "$service_user"
service_uid="$(id -u "$service_user")"
service_gid="$(id -g "$service_user")"
backup_dir="$test_root/backups"
install -d -o root -g root -m 0700 "$backup_dir"
backup_device="$(stat -c '%d' -- "$backup_dir")"

create_staging() {
  local staging
  chown "root:$service_gid" "$backup_dir"
  chmod 0710 "$backup_dir"
  staging="$(mktemp -d "$backup_dir/.noyra-staging.XXXXXX")"
  printf '%s\n' 'noyra-upgrade-staging-v1' > "$staging/.noyra-staging-marker"
  chown root:root "$staging/.noyra-staging-marker"
  chmod 0600 "$staging/.noyra-staging-marker"
  install -o root -g root -m 0600 /dev/null -- "$staging/.noyra-staging.lock"
  chown "root:$service_gid" "$staging"
  chmod 1770 "$staging"
  printf '%s\n' "$staging"
}

# Model the installer's staged handoff with a real non-root service account.
staging="$(create_staging)"
flock --exclusive -- "$staging/.noyra-staging.lock" runuser --user="$service_user" --group="$service_user" -- \
  /bin/bash -c 'umask 077; printf payload > "$1/backup.noyra-backup"' _ "$staging"
flock --exclusive -- "$staging/.noyra-staging.lock" runuser --user="$service_user" --group="$service_user" -- \
  python3 -c 'import os, sys; fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)' "$staging"
staged_backup="$staging/backup.noyra-backup"
expected="$service_uid:$service_gid:600:1:$backup_device"
[[ "$(stat -c '%u:%g:%a:%h:%d' -- "$staged_backup")" == "$expected" ]] || \
  fail 'staged backup metadata does not match the service-account contract'

chown root:root "$staging"
chmod 0700 "$staging"
chown root:root "$staged_backup"
chmod 0600 "$staged_backup"
mv -Tf -- "$staged_backup" "$backup_dir/final.noyra-backup"
rm -f -- "$staging/.noyra-staging.lock"
rm -f -- "$staging/.noyra-staging-marker"
rmdir -- "$staging"
chown root:root "$backup_dir"
chmod 0700 "$backup_dir"
[[ "$(stat -c '%u:%g:%a:%h' -- "$backup_dir/final.noyra-backup")" == '0:0:600:1' ]] || \
  fail 'published backup metadata is unsafe'
[[ "$(stat -c '%u:%g:%a' -- "$backup_dir")" == '0:0:700' ]] || \
  fail 'backup directory did not return to the root-only contract'

# A hard-killed upgrade leaves a root marker in a service-writable directory;
# the next run can identify that exact state without deleting operator data.
stale="$(create_staging)"
candidate_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$stale")"
marker_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$stale/.noyra-staging-marker")"
lock_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$stale/.noyra-staging.lock")"
[[ "$candidate_metadata" == "0:$service_gid:1770:2:$backup_device" ]] || \
  fail 'stale staging directory is not recognizable'
[[ "$marker_metadata" == "0:0:600:1:$backup_device" ]] || \
  fail 'stale staging marker is not trustworthy'
[[ "$lock_metadata" == "0:0:600:1:$backup_device" ]] || \
  fail 'stale staging lock is not trustworthy'
[[ "$(<"$stale/.noyra-staging-marker")" == 'noyra-upgrade-staging-v1' ]] || \
  fail 'stale staging marker content is invalid'
flock --exclusive -- "$stale/.noyra-staging.lock" sleep 1 &
lock_pid="$!"
sleep 0.1
if flock -n -- "$stale/.noyra-staging.lock" /usr/bin/true; then
  kill "$lock_pid" >/dev/null 2>&1 || true
  wait "$lock_pid" || true
  fail 'active staging lock was not observed'
fi
wait "$lock_pid"
lock_pid=""
rm -rf --one-file-system -- "$stale"
chown root:root "$backup_dir"
chmod 0700 "$backup_dir"
[[ ! -e "$stale" ]] || fail 'recognized stale staging was not removed'

operator_dir="$backup_dir/.noyra-staging.operator"
mkdir "$operator_dir"
chmod 0700 "$operator_dir"
[[ ! -e "$operator_dir/.noyra-staging-marker" ]] || fail 'operator control directory is invalid'
[[ -d "$operator_dir" ]] || fail 'unmarked operator directory was removed'

echo 'installer backup boundary checks passed'
