#!/usr/bin/env bash
set -Eeuo pipefail

# Provision a fresh LUKS2 image on the existing filesystem. This helper is
# deliberately separate from install-ubuntu.sh because initialization can
# destroy an existing path and must never happen as an install side effect.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SIZE=10G
IMAGE=/var/lib/noyra-data.img
MAPPER=noyra-data
MOUNT_POINT=/var/lib/noyra
DRY_RUN=false
CONFIRMED=false
LOOP_DEVICE=''
CREATED_IMAGE=false
MOUNTED=false
MAPPER_OPEN=false
CREATED_MOUNT_POINT=false
CONFIG_DIR_CREATED=false
DROPDIR_CREATED=false
CREATED_CONFIG=false
CREATED_DROPDIR=false
CREATED_HELPER=false
TEMPORARY_PATH=''
CONFIG_DIR=/etc/noyra
CONFIG_PATH=/etc/noyra/storage.conf
DROPIN_DIR=/etc/systemd/system/noyra.service.d
DROPIN_PATH=/etc/systemd/system/noyra.service.d/storage.conf
HELPER_PATH=/usr/local/sbin/noyra-storage-unlock

usage() {
  cat <<'EOF'
Usage: setup-ubuntu-single-disk-storage.sh [options]

Creates and mounts a fresh LUKS2/ext4 image on the existing system disk.
The passphrase is entered interactively by cryptsetup and is not stored.

Options:
  --size SIZE       Image size accepted by numfmt, default: 10G
  --image PATH      Absolute image path, default: /var/lib/noyra-data.img
  --mapper NAME     Device-mapper name, default: noyra-data
  --mount-point PATH Must remain /var/lib/noyra for the native service
  --yes             Confirm creation without the interactive CREATE prompt
  --dry-run         Validate and print the plan without changing the host
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size)
      [[ $# -ge 2 ]] || fail 'Missing value for --size.'
      SIZE="$2"
      shift 2
      ;;
    --image)
      [[ $# -ge 2 ]] || fail 'Missing value for --image.'
      IMAGE="$2"
      shift 2
      ;;
    --mapper)
      [[ $# -ge 2 ]] || fail 'Missing value for --mapper.'
      MAPPER="$2"
      shift 2
      ;;
    --mount-point)
      [[ $# -ge 2 ]] || fail 'Missing value for --mount-point.'
      MOUNT_POINT="$2"
      shift 2
      ;;
    --yes)
      CONFIRMED=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

require_commands() {
  local command
  for command in cryptsetup losetup truncate mkfs.ext4 mount umount mountpoint findmnt numfmt stat install flock systemctl realpath df awk find getent chown chmod rm mktemp; do
    command -v "$command" >/dev/null 2>&1 || fail "Required command is unavailable: $command"
  done
}

validate_size() {
  local bytes
  bytes="$(numfmt --from=iec "$SIZE" 2>/dev/null)" || fail "Invalid image size: $SIZE"
  [[ "$bytes" =~ ^[0-9]+$ ]] || fail "Invalid image size: $SIZE"
  (( bytes >= 1073741824 )) || fail 'Image size must be at least 1 GiB.'
  (( bytes <= 100000000000 )) || fail 'Image size must not exceed 100 GB.'
  IMAGE_BYTES="$bytes"
}

validate_absolute_path() {
  local path="$1" label="$2" parent canonical_parent base
  [[ "$path" = /* ]] || fail "$label must be an absolute path: $path"
  parent="$(dirname -- "$path")"
  base="$(basename -- "$path")"
  [[ "$base" =~ ^[A-Za-z0-9._-]+$ ]] || fail "$label has an unsafe filename: $base"
  [[ -d "$parent" && ! -L "$parent" ]] || fail "$label parent must be a real directory: $parent"
  canonical_parent="$(realpath -e -- "$parent")" || fail "$label parent cannot be resolved: $parent"
  [[ "$canonical_parent" == "$parent" ]] || fail "$label parent must not contain symlinks: $parent"
  [[ "$(stat -c '%u:%g' -- "$parent")" == '0:0' ]] || fail "$label parent must be root-owned: $parent"
  case "$(stat -c '%a' -- "$parent")" in
    *[2367][0-7]|*[0-7][2367]) fail "$label parent must not be group/other writable: $parent" ;;
  esac
}

validate_mount_point() {
  [[ "$MOUNT_POINT" == /var/lib/noyra ]] || fail 'The native service requires --mount-point /var/lib/noyra.'
  if mountpoint -q "$MOUNT_POINT"; then
    fail "Mount point is already mounted: $MOUNT_POINT"
  fi
  if [[ -e "$MOUNT_POINT" || -L "$MOUNT_POINT" ]]; then
    [[ -d "$MOUNT_POINT" && ! -L "$MOUNT_POINT" ]] || fail "Mount point must be a real directory: $MOUNT_POINT"
    [[ "$(stat -c '%u' -- "$MOUNT_POINT")" =~ ^[0-9]+$ ]] || fail "Could not inspect mount point: $MOUNT_POINT"
  elif [[ "$DRY_RUN" == true ]]; then
    return 0
  else
    return 0
  fi
  [[ -z "$(find "$MOUNT_POINT" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    fail "Mount point must be empty before initialization: $MOUNT_POINT"
  }
}

prepare_mount_point() {
  [[ -d "$MOUNT_POINT" ]] && return 0
  install -d -o root -g root -m 0755 -- "$MOUNT_POINT"
  CREATED_MOUNT_POINT=true
}

validate_space() {
  local available required
  available="$(df -Pk -- "$(dirname -- "$IMAGE")" | awk 'NR == 2 {print $4 * 1024}')"
  required=$((IMAGE_BYTES + 2147483648))
  [[ "$available" =~ ^[0-9]+$ ]] || fail 'Could not determine free space.'
  (( available >= required )) || fail "Need at least $((required / 1073741824)) GiB free on the backing filesystem."
}

validate_install_targets() {
  local path config_mode dropin_mode
  if [[ -e "$CONFIG_DIR" || -L "$CONFIG_DIR" ]]; then
    [[ -d "$CONFIG_DIR" && ! -L "$CONFIG_DIR" ]] || fail "Configuration directory must be a real directory: $CONFIG_DIR"
    [[ "$(stat -c '%u' -- "$CONFIG_DIR")" == '0' ]] || fail "Configuration directory must be root-owned: $CONFIG_DIR"
    config_mode="$(stat -c '%a' -- "$CONFIG_DIR")"
    case "$config_mode" in *[2367][0-7]|*[0-7][2367]) fail "Configuration directory must not be group/other writable: $CONFIG_DIR" ;; esac
  elif [[ "$DRY_RUN" != true ]]; then
    CONFIG_DIR_CREATED=true
  fi
  for path in "$CONFIG_PATH" "$DROPIN_PATH" "$HELPER_PATH"; do
    [[ ! -e "$path" && ! -L "$path" ]] || fail "Refusing to overwrite existing path: $path"
  done
  if [[ -e "$DROPIN_DIR" || -L "$DROPIN_DIR" ]]; then
    [[ -d "$DROPIN_DIR" && ! -L "$DROPIN_DIR" ]] || fail "Systemd drop-in directory must be real: $DROPIN_DIR"
    [[ "$(stat -c '%u:%g' -- "$DROPIN_DIR")" == '0:0' ]] || fail "Systemd drop-in directory must be root-owned: $DROPIN_DIR"
    dropin_mode="$(stat -c '%a' -- "$DROPIN_DIR")"
    case "$dropin_mode" in *[2367][0-7]|*[0-7][2367]) fail "Systemd drop-in directory must not be group/other writable: $DROPIN_DIR" ;; esac
  elif [[ "$DRY_RUN" != true ]]; then
    DROPDIR_CREATED=true
  fi
}

write_config_and_dropin() {
  local temporary
  if [[ ! -d "$CONFIG_DIR" ]]; then
    install -d -o root -g root -m 0700 -- "$CONFIG_DIR"
  fi
  if [[ ! -d "$DROPIN_DIR" ]]; then
    install -d -o root -g root -m 0755 -- "$DROPIN_DIR"
  fi
  temporary="$(mktemp "$CONFIG_DIR/.storage.conf.tmp.XXXXXX")"
  TEMPORARY_PATH="$temporary"
  printf 'NOYRA_STORAGE_IMAGE=%s\nNOYRA_STORAGE_MAPPER=%s\nNOYRA_STORAGE_MOUNT_POINT=%s\n' \
    "$IMAGE" "$MAPPER" "$MOUNT_POINT" > "$temporary"
  chown root:root -- "$temporary"
  chmod 0600 -- "$temporary"
  mv -Tf -- "$temporary" "$CONFIG_PATH"
  TEMPORARY_PATH=''
  CREATED_CONFIG=true
  install -o root -g root -m 0755 -- "$SCRIPT_DIR/unlock-ubuntu-single-disk-storage.sh" "$HELPER_PATH"
  CREATED_HELPER=true
  temporary="$(mktemp "$DROPIN_DIR/.storage.conf.tmp.XXXXXX")"
  TEMPORARY_PATH="$temporary"
  printf '%s\n' '[Unit]' 'ConditionPathIsMountPoint=/var/lib/noyra' > "$temporary"
  chown root:root -- "$temporary"
  chmod 0644 -- "$temporary"
  mv -Tf -- "$temporary" "$DROPIN_PATH"
  TEMPORARY_PATH=''
  CREATED_DROPDIR=true
  systemctl daemon-reload
}

cleanup_on_error() {
  local status=$?
  trap - EXIT
  [[ -n "$TEMPORARY_PATH" ]] && rm -f -- "$TEMPORARY_PATH"
  if [[ "$MOUNTED" == true ]]; then
    umount "$MOUNT_POINT" >/dev/null 2>&1 || true
  fi
  if [[ "$MAPPER_OPEN" == true ]]; then
    cryptsetup close "$MAPPER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$LOOP_DEVICE" ]]; then
    losetup -d "$LOOP_DEVICE" >/dev/null 2>&1 || true
  fi
  if [[ "$CREATED_IMAGE" == true ]]; then
    rm -f -- "$IMAGE"
  fi
  [[ "$CREATED_CONFIG" == true ]] && rm -f -- "$CONFIG_PATH"
  [[ "$CREATED_HELPER" == true ]] && rm -f -- "$HELPER_PATH"
  [[ "$CREATED_DROPDIR" == true ]] && rm -f -- "$DROPIN_PATH"
  [[ "${DROPDIR_CREATED:-false}" == true ]] && rmdir -- "$DROPIN_DIR" >/dev/null 2>&1 || true
  [[ "${CONFIG_DIR_CREATED:-false}" == true ]] && rmdir -- "$CONFIG_DIR" >/dev/null 2>&1 || true
  [[ "$CREATED_MOUNT_POINT" == true ]] && rmdir -- "$MOUNT_POINT" >/dev/null 2>&1 || true
  exit "$status"
}

require_commands
validate_size
[[ "$IMAGE" = /* ]] || fail "Image path must be an absolute path: $IMAGE"
[[ "$IMAGE" != "$MOUNT_POINT" && "$IMAGE" != "$MOUNT_POINT"/* ]] || fail 'Image must be outside the data mount.'
validate_absolute_path "$IMAGE" 'Image path'
validate_absolute_path "$HELPER_PATH" 'Helper path'
validate_mount_point
validate_space
[[ ! -e "$IMAGE" && ! -L "$IMAGE" ]] || fail "Refusing to overwrite an existing image: $IMAGE"
[[ "$MAPPER" =~ ^[A-Za-z0-9._-]+$ ]] || fail 'Mapper name is invalid.'
if systemctl is-active --quiet noyra; then
  fail 'Stop the noyra service before initializing the data volume.'
fi
if cryptsetup status "$MAPPER" >/dev/null 2>&1; then
  fail "Mapper is already active; refusing to use it: $MAPPER"
fi
validate_install_targets

echo "Plan: create a fresh $SIZE LUKS2 image at $IMAGE and mount it at $MOUNT_POINT."
echo 'The image will be initialized and any existing target must remain untouched.'
if [[ "$DRY_RUN" == true ]]; then
  echo 'Dry run: no host changes were made.'
  exit 0
fi
if [[ "$CONFIRMED" != true ]]; then
  read -r -p 'Type CREATE to continue: ' confirmation
  [[ "$confirmation" == CREATE ]] || fail 'Initialization was not confirmed.'
fi
[[ -t 0 && -t 1 ]] || fail 'Interactive passphrase setup requires a terminal.'

trap cleanup_on_error EXIT
prepare_mount_point
if ! (set -o noclobber; : > "$IMAGE"); then
  fail "Refusing to create or overwrite the image: $IMAGE"
fi
CREATED_IMAGE=true
truncate -s "$SIZE" -- "$IMAGE"
chown root:root -- "$IMAGE"
chmod 0600 -- "$IMAGE"
LOOP_DEVICE="$(losetup --find --show -- "$IMAGE")"
cryptsetup luksFormat --type luks2 --batch-mode --verify-passphrase "$LOOP_DEVICE"
cryptsetup open "$LOOP_DEVICE" "$MAPPER"
MAPPER_OPEN=true
mkfs.ext4 -L noyra-data "/dev/mapper/$MAPPER"
mount "/dev/mapper/$MAPPER" "$MOUNT_POINT"
MOUNTED=true
if getent passwd noyra >/dev/null 2>&1 && getent group noyra >/dev/null 2>&1; then
  chown noyra:noyra -- "$MOUNT_POINT"
else
  echo 'Service account noyra is not installed yet; install-ubuntu.sh will assign mount ownership.'
fi
chmod 0700 -- "$MOUNT_POINT"
write_config_and_dropin

trap - EXIT
echo 'Single-disk encrypted storage is ready.'
echo "Unlock after reboot: sudo /usr/local/sbin/noyra-storage-unlock"
echo "Lock before maintenance: sudo /usr/local/sbin/noyra-storage-unlock --lock"
echo 'Do not store the LUKS passphrase in /etc, shell history, or the same server.'
