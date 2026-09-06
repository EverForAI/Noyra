#!/usr/bin/env bash
set -Eeuo pipefail

# Unlock or lock the explicit single-disk LUKS container created by the
# provisioning helper. The passphrase is always read by cryptsetup and is
# never written to the host.

CONFIG_FILE=/etc/noyra/storage.conf
IMAGE=/var/lib/noyra-data.img
MAPPER=noyra-data
MOUNT_POINT=/var/lib/noyra
LOCK_MODE=false
LOOP_DEVICE=''
LOOP_CREATED=false
MAPPER_OPENED=false
MOUNTED_BY_US=false
CREATED_MOUNT_POINT=false

usage() {
  cat <<'EOF'
Usage: unlock-ubuntu-single-disk-storage.sh [--config PATH] [--lock]

Unlocks and mounts the configured LUKS image, or with --lock stops Noyra,
unmounts the data volume, closes the mapping, and detaches its loop device.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo 'Missing value for --config.' >&2; exit 2; }
      CONFIG_FILE="$2"
      shift 2
      ;;
    --lock)
      LOCK_MODE=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || {
  echo 'Run this helper as root.' >&2
  exit 1
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_commands() {
  local command
  for command in cryptsetup losetup mount umount findmnt mountpoint flock stat realpath dirname basename install systemctl id getent awk; do
    command -v "$command" >/dev/null 2>&1 || fail "Required command is unavailable: $command"
  done
}

validate_secure_parent() {
  local path="$1" label="$2" parent canonical mode
  parent="$(dirname -- "$path")"
  [[ -d "$parent" && ! -L "$parent" ]] || fail "$label parent must be a real directory: $parent"
  canonical="$(realpath -e -- "$parent")" || fail "$label parent cannot be resolved: $parent"
  [[ "$canonical" == "$parent" ]] || fail "$label parent must not contain symlinks: $parent"
  [[ "$(stat -c '%u' -- "$parent")" == '0' ]] || fail "$label parent must be root-owned: $parent"
  mode="$(stat -c '%a' -- "$parent")"
  case "$mode" in
    *[2367][0-7]|*[0-7][2367]) fail "$label parent must not be group/other writable: $parent" ;;
  esac
}

load_config() {
  local key value
  [[ "$CONFIG_FILE" = /* ]] || fail "Configuration path must be absolute: $CONFIG_FILE"
  [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" ]] || fail "Storage configuration is unavailable: $CONFIG_FILE"
  validate_secure_parent "$CONFIG_FILE" 'Configuration path'
  [[ "$(stat -c '%u:%a:%h' -- "$CONFIG_FILE")" == '0:600:1' ||
    "$(stat -c '%u:%a:%h' -- "$CONFIG_FILE")" == '0:640:1' ]] ||
    fail "Storage configuration must be root-owned mode 0600 or 0640: $CONFIG_FILE"

  local seen_image=false seen_mapper=false seen_mount=false
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    [[ -n "$key" ]] || continue
    [[ "$value" != *$'\r'* ]] || fail 'Storage configuration must use Unix line endings.'
    case "$key" in
      NOYRA_STORAGE_IMAGE)
        [[ "$seen_image" == false ]] || fail 'Duplicate storage image setting.'
        IMAGE="$value"; seen_image=true
        ;;
      NOYRA_STORAGE_MAPPER)
        [[ "$seen_mapper" == false ]] || fail 'Duplicate storage mapper setting.'
        MAPPER="$value"; seen_mapper=true
        ;;
      NOYRA_STORAGE_MOUNT_POINT)
        [[ "$seen_mount" == false ]] || fail 'Duplicate storage mount setting.'
        MOUNT_POINT="$value"; seen_mount=true
        ;;
      *) fail "Unexpected storage configuration key: $key" ;;
    esac
  done < "$CONFIG_FILE"
  [[ "$seen_image" == true && "$seen_mapper" == true && "$seen_mount" == true ]] ||
    fail 'Storage configuration is incomplete.'
  [[ "$IMAGE" = /* && "$MOUNT_POINT" = /* ]] || fail 'Storage paths must be absolute.'
  [[ "$MAPPER" =~ ^[A-Za-z0-9._-]+$ && "$MAPPER" != '.' && "$MAPPER" != '..' ]] ||
    fail 'Storage mapper name is invalid.'
  [[ "$MOUNT_POINT" == /var/lib/noyra ]] ||
    fail 'The native service requires /var/lib/noyra as its data mount.'
}

validate_image() {
  local parent base
  parent="$(dirname -- "$IMAGE")"
  base="$(basename -- "$IMAGE")"
  [[ "$base" =~ ^[A-Za-z0-9._-]+$ ]] || fail "LUKS image has an unsafe filename: $base"
  validate_secure_parent "$IMAGE" 'LUKS image path'
  [[ "$IMAGE" != "$MOUNT_POINT" && "$IMAGE" != "$MOUNT_POINT"/* ]] ||
    fail 'LUKS image must be outside the data mount.'
  [[ -f "$IMAGE" && ! -L "$IMAGE" ]] || fail "LUKS image is not a regular file: $IMAGE"
  [[ "$(stat -c '%u:%g:%a:%h' -- "$IMAGE")" == '0:0:600:1' ]] ||
    fail "LUKS image must be root:root mode 0600: $IMAGE"
}

ensure_mount_point() {
  if mountpoint -q "$MOUNT_POINT"; then
    return 0
  fi
  if [[ -e "$MOUNT_POINT" || -L "$MOUNT_POINT" ]]; then
    [[ -d "$MOUNT_POINT" && ! -L "$MOUNT_POINT" ]] || fail "Mount point must be a real directory: $MOUNT_POINT"
  else
    install -d -o root -g root -m 0755 -- "$MOUNT_POINT"
    CREATED_MOUNT_POINT=true
  fi
}

require_service_account() {
  id -u noyra >/dev/null 2>&1 || fail 'The noyra service account is unavailable.'
  getent group noyra >/dev/null 2>&1 || fail 'The noyra service group is unavailable.'
}

loop_device_for_image() {
  local matches
  matches="$(losetup -j "$IMAGE" || true)"
  [[ -z "$matches" ]] && return 0
  [[ "$(printf '%s\n' "$matches" | awk 'NF {count++} END {print count + 0}')" == '1' ]] ||
    fail "Multiple loop devices are attached to the LUKS image: $IMAGE"
  printf '%s\n' "$matches" | awk -F: 'NF {print $1; exit}'
}

mapper_is_active() {
  cryptsetup status "$MAPPER" >/dev/null 2>&1
}

cleanup_on_error() {
  local status=$?
  trap - EXIT
  if [[ "$MOUNTED_BY_US" == true ]]; then
    umount "$MOUNT_POINT" >/dev/null 2>&1 || true
  fi
  if [[ "$MAPPER_OPENED" == true ]]; then
    cryptsetup close "$MAPPER" >/dev/null 2>&1 || true
  fi
  if [[ "$LOOP_CREATED" == true && -n "$LOOP_DEVICE" ]]; then
    losetup -d "$LOOP_DEVICE" >/dev/null 2>&1 || true
  fi
  [[ "$CREATED_MOUNT_POINT" == true ]] && rmdir -- "$MOUNT_POINT" >/dev/null 2>&1 || true
  exit "$status"
}

lock_storage() {
  if systemctl is-active --quiet noyra; then
    systemctl stop noyra || fail 'Could not stop the noyra service.'
  fi
  if mountpoint -q "$MOUNT_POINT"; then
    findmnt -rn -S "/dev/mapper/$MAPPER" -T "$MOUNT_POINT" >/dev/null ||
      fail "The data mount is occupied by another device: $MOUNT_POINT"
    umount -- "$MOUNT_POINT"
  fi
  if mapper_is_active; then
    cryptsetup close "$MAPPER"
  fi
  LOOP_DEVICE="$(loop_device_for_image)"
  if [[ -n "$LOOP_DEVICE" ]]; then
    losetup -d "$LOOP_DEVICE"
    [[ -z "$(losetup -j "$IMAGE")" ]] || fail "Loop device is still attached: $LOOP_DEVICE"
  fi
  echo "Locked Noyra storage: $IMAGE"
}

unlock_storage() {
  local existing_loop
  trap cleanup_on_error EXIT
  ensure_mount_point
  if mountpoint -q "$MOUNT_POINT"; then
    findmnt -rn -S "/dev/mapper/$MAPPER" -T "$MOUNT_POINT" >/dev/null ||
      fail "The data mount is occupied by another device: $MOUNT_POINT"
    systemctl start noyra
    echo "Noyra storage is already mounted: $MOUNT_POINT"
    return 0
  fi
  if mapper_is_active; then
    fail "Mapper is already active while the data mount is unavailable: $MAPPER"
  fi
  require_service_account
  [[ -t 0 && -t 1 ]] || fail 'Interactive passphrase unlock requires a terminal.'
  existing_loop="$(loop_device_for_image)"
  if [[ -n "$existing_loop" ]]; then
    LOOP_DEVICE="$existing_loop"
  else
    LOOP_DEVICE="$(losetup --find --show -- "$IMAGE")"
    LOOP_CREATED=true
  fi
  cryptsetup open "$LOOP_DEVICE" "$MAPPER"
  MAPPER_OPENED=true
  mount "/dev/mapper/$MAPPER" "$MOUNT_POINT"
  MOUNTED_BY_US=true
  chown noyra:noyra -- "$MOUNT_POINT"
  chmod 0700 -- "$MOUNT_POINT"
  trap - EXIT
  if ! systemctl start noyra; then
    echo 'Storage is mounted, but the noyra service failed to start.' >&2
    exit 1
  fi
  echo "Unlocked and mounted Noyra storage: $MOUNT_POINT"
}

require_commands
load_config
validate_image
exec 9>/run/lock/noyra-storage.lock
flock -n 9 || fail 'Another Noyra storage operation is already running.'

if [[ "$LOCK_MODE" == true ]]; then
  lock_storage
else
  unlock_storage
fi
