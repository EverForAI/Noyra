#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || {
  echo 'single-disk storage contract checks skipped: root is required'
  exit 0
}

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
setup_script="$repo_root/scripts/setup-ubuntu-single-disk-storage.sh"
unlock_script="$repo_root/scripts/unlock-ubuntu-single-disk-storage.sh"
bash -n "$setup_script"
bash -n "$unlock_script"

test_root="$(mktemp -d /tmp/noyra-storage-contract.XXXXXX)"
stub_dir="$test_root/stubs"
mkdir "$stub_dir"
mount_point_created=false
cleanup() {
  if [[ "$mount_point_created" == true ]]; then
    rm -f -- /var/lib/noyra/.unexpected-entry
    rmdir -- /var/lib/noyra 2>/dev/null || true
  fi
  rm -rf --one-file-system "$test_root"
}
trap cleanup EXIT

stub() {
  local name="$1" body="$2"
  printf '#!/usr/bin/env bash\n%s\n' "$body" > "$stub_dir/$name"
  chmod 0755 "$stub_dir/$name"
}

# The dry-run must only inspect the host. These stubs make the test independent
# of whether cryptsetup or systemd is installed on the CI runner.
stub cryptsetup 'exit 4'
stub losetup '[[ "${1:-}" == -j ]] && exit 0; exit 1'
stub mountpoint 'exit 1'
stub find 'exit 0'
stub findmnt 'exit 1'
stub mount 'exit 1'
stub umount 'exit 0'
stub systemctl '[[ "${1:-}" == is-active ]] && exit 3; exit 0'
stub df 'printf "%s\n" "Filesystem 1024-blocks Used Available Capacity Mounted on" "contract 100000000 0 100000000 0%% /"'

run_setup() {
  PATH="$stub_dir:$PATH" bash "$setup_script" --dry-run --yes --size 1G --image "$1" >"$test_root/stdout" 2>"$test_root/stderr"
}

valid_image="$test_root/valid.img"
run_setup "$valid_image" || fail 'valid dry-run was rejected'
grep -F 'Dry run: no host changes were made.' "$test_root/stdout" >/dev/null || fail 'dry-run confirmation missing'
[[ ! -e "$valid_image" ]] || fail 'dry-run created the image'

if run_setup relative.img; then
  fail 'relative image path was accepted'
fi
grep -F 'must be an absolute path' "$test_root/stderr" >/dev/null || fail 'relative-path error was not reported'

existing_image="$test_root/existing.img"
touch "$existing_image"
if run_setup "$existing_image"; then
  fail 'existing image was accepted'
fi
grep -F 'Refusing to overwrite an existing image' "$test_root/stderr" >/dev/null || fail 'existing-image error was not reported'

if run_setup /var/lib/noyra/contract.img; then
  fail 'image below the mount point was accepted'
fi
grep -F 'must be outside the data mount' "$test_root/stderr" >/dev/null || fail 'mount containment error was not reported'

# Model an existing, non-empty native mount point. The setup script intentionally
# permits a missing mount point during dry-run so first-install validation can be
# inspected without mutating the host.
if [[ ! -e /var/lib/noyra ]]; then
  mkdir -p /var/lib/noyra
  mount_point_created=true
fi
stub find 'printf "%s\\n" /var/lib/noyra/.unexpected-entry'
if run_setup "$test_root/nonempty.img"; then
  fail 'non-empty mount point was accepted'
fi
grep -F 'Mount point must be empty' "$test_root/stderr" >/dev/null || fail 'non-empty mount error was not reported'
if [[ "$mount_point_created" == true ]]; then
  rmdir /var/lib/noyra
  mount_point_created=false
fi

storage_image="$test_root/storage.img"
printf 'placeholder' > "$storage_image"
chmod 0600 "$storage_image"
config="$test_root/storage.conf"
printf 'NOYRA_STORAGE_IMAGE=%s\nNOYRA_STORAGE_MAPPER=noyra-data\nNOYRA_STORAGE_MOUNT_POINT=/var/lib/noyra\n' \
  "$storage_image" > "$config"
chmod 0600 "$config"
PATH="$stub_dir:$PATH" bash "$unlock_script" --config "$config" --lock >"$test_root/unlock.stdout" 2>"$test_root/unlock.stderr" ||
  fail 'valid lock operation was rejected'
grep -F 'Locked Noyra storage' "$test_root/unlock.stdout" >/dev/null || fail 'lock confirmation missing'

chmod 0644 "$config"
if PATH="$stub_dir:$PATH" bash "$unlock_script" --config "$config" --lock >"$test_root/unlock.stdout" 2>"$test_root/unlock.stderr"; then
  fail 'unsafe configuration mode was accepted'
fi
grep -F 'must be root-owned mode 0600 or 0640' "$test_root/unlock.stderr" >/dev/null || fail 'unsafe configuration error was not reported'

chmod 0600 "$config"
ln -s "$config" "$test_root/config-link"
if PATH="$stub_dir:$PATH" bash "$unlock_script" --config "$test_root/config-link" --lock >"$test_root/unlock.stdout" 2>"$test_root/unlock.stderr"; then
  fail 'configuration symlink was accepted'
fi
grep -F 'Storage configuration is unavailable' "$test_root/unlock.stderr" >/dev/null || fail 'configuration symlink error was not reported'

echo 'single-disk storage contract checks passed'
