#!/usr/bin/env bash
set -euo pipefail

# Ubuntu installs are release based. A failed pip install never mutates the
# release selected by systemd; current/previous are replaced only after the
# staged virtualenv and import checks have completed.

profile="${NOYRA_INSTALL_PROFILE:-base}"
release_id="${NOYRA_RELEASE_ID:-}"
rollback=false
ready_timeout="${NOYRA_READY_TIMEOUT_SECONDS:-60}"
backup_dir="${NOYRA_UPGRADE_BACKUP_DIR:-/var/backups/noyra}"
backup_dir_default=false
if [[ -z "${NOYRA_UPGRADE_BACKUP_DIR+x}" ]]; then
  backup_dir_default=true
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo 'Missing value for --profile.' >&2; exit 2; }
      profile="$2"
      shift 2
      ;;
    --release-id)
      [[ $# -ge 2 ]] || { echo 'Missing value for --release-id.' >&2; exit 2; }
      release_id="$2"
      shift 2
      ;;
    --ready-timeout)
      [[ $# -ge 2 ]] || { echo 'Missing value for --ready-timeout.' >&2; exit 2; }
      ready_timeout="$2"
      shift 2
      ;;
    --backup-dir)
      [[ $# -ge 2 ]] || { echo 'Missing value for --backup-dir.' >&2; exit 2; }
      backup_dir="$2"
      backup_dir_default=false
      shift 2
      ;;
    --rollback)
      rollback=true
      shift
      ;;
    *)
      echo "Unknown installer argument: $1" >&2
      exit 2
      ;;
  esac
done
case "$profile" in
  base|cloud) ;;
  *) echo 'Install profile must be base or cloud.' >&2; exit 2 ;;
esac
if [[ ! "$ready_timeout" =~ ^[0-9]+$ ]] || (( ready_timeout < 1 || ready_timeout > 1800 )); then
  echo '--ready-timeout must be an integer between 1 and 1800 seconds.' >&2
  exit 2
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
command -v flock >/dev/null 2>&1 || { echo 'flock is required (install util-linux).' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo 'curl is required for the readiness check.' >&2; exit 1; }

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/noyra
RELEASES_DIR="$INSTALL_DIR/releases"
CURRENT_LINK="$INSTALL_DIR/current"
PREVIOUS_LINK="$INSTALL_DIR/previous"
LOCK_FILE="$INSTALL_DIR/.install.lock"
DATA_DIR=/var/lib/noyra
CONFIG_DIR=/etc/noyra
UNIT_FILE=/etc/systemd/system/noyra.service
PROFILE_DROPIN_DIR=/etc/systemd/system/noyra.service.d
PROFILE_DROPIN="$PROFILE_DROPIN_DIR/profile.conf"
BACKUP_KEYRING="$CONFIG_DIR/backup-keyring.json"

assert_absolute_backup_dir() {
  local parent base canonical_parent current component metadata perm remainder
  local -a components
  command -v realpath >/dev/null 2>&1 || {
    echo 'realpath is required to validate the upgrade backup boundary.' >&2
    return 1
  }
  [[ "$backup_dir" = /* ]] || { echo '--backup-dir must be absolute.' >&2; return 1; }
  while [[ "$backup_dir" != "/" && "$backup_dir" == */ ]]; do
    backup_dir="${backup_dir%/}"
  done
  [[ "$backup_dir" != "/" ]] || {
    echo '--backup-dir must name a normal directory.' >&2
    return 1
  }
  parent="${backup_dir%/*}"
  base="${backup_dir##*/}"
  [[ -n "$base" && "$base" != "." && "$base" != ".." ]] || {
    echo '--backup-dir must name a normal directory.' >&2
    return 1
  }
  [[ -n "$parent" ]] || parent="/"
  [[ -d "$parent" && ! -L "$parent" ]] || {
    echo '--backup-dir parent must already be a real directory.' >&2
    return 1
  }
  # Refuse dot traversal, links and any ancestor writable by a non-root
  # account. This keeps later root chown/chmod operations inside a trusted
  # operator-provisioned boundary.
  current="/"
  remainder="${parent#/}"
  IFS='/' read -r -a components <<< "$remainder"
  for component in "${components[@]}"; do
    [[ -n "$component" ]] || continue
    [[ "$component" != "." && "$component" != ".." ]] || {
      echo '--backup-dir cannot contain dot traversal.' >&2
      return 1
    }
    current="${current%/}/$component"
    [[ ! -L "$current" ]] || {
      echo "Backup directory path cannot contain a symlink: $current" >&2
      return 1
    }
    [[ -d "$current" ]] || {
      echo "Backup directory path component is not a directory: $current" >&2
      return 1
    }
    metadata="$(stat -c '%u:%a' -- "$current" 2>/dev/null || true)"
    perm="${metadata#*:}"
    [[ "$metadata" =~ ^0:[0-7]{3}$ ]] || {
      echo "Backup directory path must be root-owned and not group/other writable: $current" >&2
      return 1
    }
    case "$perm" in
      *[2367][0-7]|*[0-7][2367])
        echo "Backup directory path must be root-owned and not group/other writable: $current" >&2
        return 1
        ;;
    esac
  done
  canonical_parent="$(realpath -e -- "$parent")" || {
    echo "Backup directory parent cannot be canonicalized: $parent" >&2
    return 1
  }
  if [[ "$canonical_parent" == "/" ]]; then
    backup_dir="/$base"
  else
    backup_dir="$canonical_parent/$base"
  fi
  case "$backup_dir" in
    "$DATA_DIR"|"$DATA_DIR"/*) echo '--backup-dir must be outside the data directory.' >&2; return 1 ;;
    "$INSTALL_DIR"|"$INSTALL_DIR"/*) echo '--backup-dir must be outside the installation directory.' >&2; return 1 ;;
  esac
  if [[ ! -e "$backup_dir" ]]; then
    [[ "$backup_dir_default" == true && "$backup_dir" == /var/backups/noyra ]] || {
      echo '--backup-dir must already exist as a real root-owned directory.' >&2
      return 1
    }
    install -d -o root -g root -m 0700 -- "$backup_dir"
  fi
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || {
    echo '--backup-dir must be a real directory.' >&2
    return 1
  }
  metadata="$(stat -c '%u:%g:%a' -- "$backup_dir" 2>/dev/null || true)"
  perm="${metadata##*:}"
  [[ "$metadata" =~ ^0:0:[0-7]{3}$ ]] || {
    echo '--backup-dir must be root:root and not group/other writable.' >&2
    return 1
  }
  case "$perm" in
    *[2367][0-7]|*[0-7][2367])
      echo '--backup-dir must be root-owned and not group/other writable.' >&2
      return 1
      ;;
  esac
}

assert_stable_backup_dir() {
  local metadata
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || {
    echo "Backup directory must remain a real directory: $backup_dir" >&2
    return 1
  }
  [[ "$(realpath -e -- "$backup_dir" 2>/dev/null || true)" == "$backup_dir" ]] || {
    echo "Backup directory identity changed during installation: $backup_dir" >&2
    return 1
  }
  metadata="$(stat -c '%u:%g:%a' -- "$backup_dir" 2>/dev/null || true)"
  [[ "$metadata" =~ ^0:0:[0-7]{3}$ ]] || {
    echo "Backup directory must remain root:root and not group/other writable: $backup_dir" >&2
    return 1
  }
  case "${metadata##*:}" in
    *[2367][0-7]|*[0-7][2367])
      echo "Backup directory must remain root-owned and not group/other writable: $backup_dir" >&2
      return 1
      ;;
  esac
}

assert_backup_dir_identity() {
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || {
    echo "Backup directory identity changed during installation: $backup_dir" >&2
    return 1
  }
  [[ "$(realpath -e -- "$backup_dir" 2>/dev/null || true)" == "$backup_dir" ]] || {
    echo "Backup directory identity changed during installation: $backup_dir" >&2
    return 1
  }
  [[ "$(stat -c '%d:%i' -- "$backup_dir" 2>/dev/null || true)" == "$backup_device:$backup_inode" ]] || {
    echo "Backup directory inode or device changed during installation: $backup_dir" >&2
    return 1
  }
}

restore_backup_dir() {
  assert_backup_dir_identity || return 1
  chown root:root -- "$backup_dir" || return 1
  chmod 0700 -- "$backup_dir" || return 1
  assert_backup_dir_identity || return 1
  [[ "$(stat -c '%u:%g:%a' -- "$backup_dir" 2>/dev/null || true)" == '0:0:700' ]]
}

id -u noyra >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin noyra
for protected_dir in "$INSTALL_DIR" "$RELEASES_DIR" "$DATA_DIR" "$CONFIG_DIR"; do
  if [[ -L "$protected_dir" ]]; then
    echo "Protected deployment directory cannot be a symlink: $protected_dir" >&2
    exit 1
  fi
done
install -d -o root -g root -m 0755 "$INSTALL_DIR" "$RELEASES_DIR"
install -d -o noyra -g noyra -m 0700 "$DATA_DIR"
install -d -o root -g noyra -m 0750 "$CONFIG_DIR"

assert_safe_segment() {
  local value="$1" name="$2"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    echo "$name contains unsafe characters: $value" >&2
    return 1
  }
}

read_pointer() {
  local pointer="$1" value target
  [[ -L "$pointer" ]] || return 1
  target="$(readlink -- "$pointer")"
  [[ "$target" == releases/* ]] || return 1
  value="${target#releases/}"
  assert_safe_segment "$value" pointer || return 1
  [[ -d "$RELEASES_DIR/$value" && -x "$RELEASES_DIR/$value/.venv/bin/python" && ! -L "$RELEASES_DIR/$value" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "$RELEASES_DIR/$value" 2>/dev/null || true)" == root:root:* ]] || return 1
  printf '%s\n' "$value"
}

validate_pointer_path() {
  local pointer="$1"
  if [[ -e "$pointer" || -L "$pointer" ]]; then
    [[ -L "$pointer" ]] || {
      echo "Release pointer must be a symlink: $pointer" >&2
      return 1
    }
  fi
}

atomic_pointer() {
  local pointer="$1" release="$2" temporary
  assert_safe_segment "$release" release
  [[ -d "$RELEASES_DIR/$release" && -x "$RELEASES_DIR/$release/.venv/bin/python" && ! -L "$RELEASES_DIR/$release" ]] || {
    echo "Release is incomplete: $release" >&2
    return 1
  }
  [[ "$(stat -c '%U:%G' "$RELEASES_DIR/$release" 2>/dev/null || true)" == root:root ]] || {
    echo "Release ownership is unsafe: $release" >&2
    return 1
  }
  temporary="$INSTALL_DIR/.$(basename -- "$pointer").tmp.$$.$RANDOM"
  rm -f -- "$temporary"
  ln -s -- "releases/$release" "$temporary"
  # mv -T is a single directory-entry replacement on the same filesystem.
  if ! mv -Tf -- "$temporary" "$pointer"; then
    rm -f -- "$temporary"
    return 1
  fi
  sync -d "$INSTALL_DIR" 2>/dev/null || sync 2>/dev/null || true
}

env_value() {
  local key="$1" fallback="$2" value
  value="$fallback"
  if [[ -f "$CONFIG_DIR/noyra.env" ]]; then
    value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$CONFIG_DIR/noyra.env")"
    [[ -n "$value" ]] || value="$fallback"
  fi
  printf '%s\n' "$value"
}

ensure_backup_keyring() {
  local python_path="$1" link_count
  if [[ -L "$BACKUP_KEYRING" || ( -e "$BACKUP_KEYRING" && ! -f "$BACKUP_KEYRING" ) ]]; then
    echo "Backup keyring must be a regular file: $BACKUP_KEYRING" >&2
    return 1
  fi
  if [[ ! -f "$BACKUP_KEYRING" ]]; then
    umask 0077
    "$python_path" -m noyra backup-key init --path "$BACKUP_KEYRING"
  fi
  link_count="$(stat -c '%h' -- "$BACKUP_KEYRING" 2>/dev/null || true)"
  [[ "$link_count" == 1 ]] || {
    echo "Backup keyring must not be hard-linked: $BACKUP_KEYRING" >&2
    return 1
  }
  chown root:noyra "$BACKUP_KEYRING"
  chmod 0640 "$BACKUP_KEYRING"
}

service_active=false
service_was_stopped=false
switched=false
old_current=""
old_previous=""
backup_path=""
backup_staging=""
backup_lock_path=""
backup_dir_exposed=false
backup_device=""
backup_inode=""
noyra_uid=""
noyra_gid=""
staging=""
cleanup_failed=false
legacy_venv=""
legacy_id=""
legacy_moved=false
release_published=false
profile_dropin_changed=false
profile_dropin_backup="$INSTALL_DIR/.profile.conf.previous.$$"
profile_dropin_backup_exists=false

cleanup_stale_backup_staging() {
  local candidate candidate_metadata marker marker_metadata marker_content lock_path lock_metadata failed=false
  assert_backup_dir_identity || return 1
  shopt -s nullglob
  for candidate in "$backup_dir"/.noyra-staging.*; do
    [[ -d "$candidate" && ! -L "$candidate" ]] || continue
    candidate_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$candidate" 2>/dev/null || true)"
    [[ -n "$candidate_metadata" ]] || {
      echo "Failed to inspect stale backup staging: $candidate" >&2
      failed=true
      break
    }
    [[ "$candidate_metadata" == "0:0:700:2:$backup_device" ||
      "$candidate_metadata" == "0:$noyra_gid:1730:2:$backup_device" ||
      "$candidate_metadata" == "0:$noyra_gid:1770:2:$backup_device" ]] || continue
    marker="$candidate/.noyra-staging-marker"
    [[ -f "$marker" && ! -L "$marker" ]] || continue
    marker_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$marker" 2>/dev/null || true)"
    [[ -n "$marker_metadata" ]] || {
      echo "Failed to inspect stale backup staging marker: $marker" >&2
      failed=true
      break
    }
    [[ "$marker_metadata" == "0:0:600:1:$backup_device" ]] || continue
    marker_content="$(<"$marker")" || {
      echo "Failed to read stale backup staging marker: $marker" >&2
      failed=true
      break
    }
    [[ "$marker_content" == "noyra-upgrade-staging-v1" ]] || continue
    lock_path="$candidate/.noyra-staging.lock"
    if [[ -e "$lock_path" || -L "$lock_path" ]]; then
      [[ -f "$lock_path" && ! -L "$lock_path" ]] || continue
      lock_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$lock_path" 2>/dev/null || true)"
      [[ "$lock_metadata" == "0:0:600:1:$backup_device" ]] || continue
      if ! flock -n -- "$lock_path" /usr/bin/true; then
        echo "Stale backup staging is still in use: $candidate" >&2
        failed=true
        break
      fi
    fi
    assert_backup_dir_identity || {
      failed=true
      break
    }
    if ! rm -rf --one-file-system -- "$candidate"; then
      echo "Failed to remove stale backup staging: $candidate" >&2
      failed=true
      break
    fi
  done
  shopt -u nullglob
  [[ "$failed" == false ]] && assert_backup_dir_identity
}

cleanup_staging() {
  local failed=false
  if [[ -n "${staging:-}" && ( -e "$staging" || -L "$staging" ) ]]; then
    if ! rm -rf --one-file-system -- "$staging"; then
      failed=true
      echo "Failed to remove release staging: $staging" >&2
    fi
  fi
  if [[ -n "${backup_staging:-}" && ( -e "$backup_staging" || -L "$backup_staging" ) ]]; then
    if ! rm -rf --one-file-system -- "$backup_staging"; then
      failed=true
      echo "Failed to remove backup staging: $backup_staging" >&2
    fi
  fi
  if [[ "$backup_dir_exposed" == true ]]; then
    if restore_backup_dir; then
      backup_dir_exposed=false
    else
      failed=true
      echo "Failed to restore root-only permissions on backup directory: $backup_dir" >&2
    fi
  fi
  if [[ "$failed" == true ]]; then
    cleanup_failed=true
  fi
}
trap cleanup_staging EXIT

validate_native_config_paths() {
  local configured_data configured_keyring
  configured_data="$(env_value NOYRA_DATA_DIR "$DATA_DIR")"
  configured_keyring="$(env_value NOYRA_BACKUP_KEYRING_PATH "$BACKUP_KEYRING")"
  [[ "$configured_data" = "$DATA_DIR" ]] || {
    echo 'NOYRA_DATA_DIR must remain /var/lib/noyra for the native Ubuntu service.' >&2
    return 1
  }
  [[ "$configured_keyring" = "$BACKUP_KEYRING" ]] || {
    echo 'NOYRA_BACKUP_KEYRING_PATH must remain /etc/noyra/backup-keyring.json for the native Ubuntu service.' >&2
    return 1
  }
}

stop_old_service() {
  if systemctl is-active --quiet noyra; then
    service_active=true
    systemctl stop noyra
    service_was_stopped=true
    local deadline=$((SECONDS + 45))
    while (( SECONDS < deadline )); do
      if ! systemctl is-active --quiet noyra; then
        return 0
      fi
      sleep 1
    done
    echo 'Noyra did not stop within 45 seconds; refusing to switch releases.' >&2
    return 1
  fi
}

start_and_check() {
  local port elapsed
  port="$(env_value NOYRA_PORT 8765)"
  [[ "$port" =~ ^[0-9]+$ ]] || { echo 'NOYRA_PORT is invalid.' >&2; return 1; }
  systemctl start noyra
  elapsed=0
  while (( elapsed < ready_timeout )); do
    if systemctl is-active --quiet noyra && \
      curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${port}/health/ready" >/dev/null; then
      return 0
    fi
    sleep 1
    ((elapsed += 1))
  done
  echo "Noyra release did not become ready within ${ready_timeout}s." >&2
  return 1
}

restore_pointers_after_failure() {
  if [[ -n "$old_current" ]]; then
    atomic_pointer "$CURRENT_LINK" "$old_current" || return 1
    [[ "$(read_pointer "$CURRENT_LINK")" == "$old_current" ]] || return 1
  elif [[ -L "$CURRENT_LINK" ]]; then
    rm -f -- "$CURRENT_LINK"
  fi
  if [[ -n "$old_previous" ]]; then
    atomic_pointer "$PREVIOUS_LINK" "$old_previous" || return 1
    [[ "$(read_pointer "$PREVIOUS_LINK")" == "$old_previous" ]] || return 1
  elif [[ -L "$PREVIOUS_LINK" ]]; then
    rm -f -- "$PREVIOUS_LINK"
  fi
}

restore_profile_dropin_after_failure() {
  [[ "$profile_dropin_changed" == true ]] || return 0
  if [[ "$profile_dropin_backup_exists" == true ]]; then
    mv -f -- "$profile_dropin_backup" "$PROFILE_DROPIN" || true
  else
    rm -f -- "$PROFILE_DROPIN"
  fi
  profile_dropin_changed=false
}

on_error() {
  local status=${1:-$?}
  trap - ERR
  # Revoke service-account access before any failure recovery restarts Noyra.
  cleanup_staging
  if [[ "$cleanup_failed" == true ]]; then
    echo 'Upgrade cleanup did not restore a safe backup boundary; leaving Noyra stopped.' >&2
    service_was_stopped=false
    status=1
  fi
  if [[ "$switched" == true ]]; then
    systemctl stop noyra >/dev/null 2>&1 || true
    local restored=true
    if ! restore_pointers_after_failure; then
      restored=false
      echo 'Failed to restore the previous release pointers; service will remain stopped.' >&2
    fi
    restore_profile_dropin_after_failure
    if [[ "$service_active" == true && "$restored" == true && "$cleanup_failed" == false ]]; then
      systemctl daemon-reload >/dev/null 2>&1 || true
      systemctl start noyra >/dev/null 2>&1 || true
    fi
    echo "Upgrade failed after pointer switch; old release ${old_current:-unknown} was restored." >&2
    if [[ -n "$backup_path" ]]; then
      echo "The data backup is at ${backup_path}. Keep the service stopped until migration compatibility is confirmed; restore data manually if the new release ran migrations." >&2
    fi
  elif [[ "$release_published" == true ]]; then
    rm -rf -- "$release_root"
    if [[ "$legacy_moved" == true ]]; then
      mkdir -p -- "$RELEASES_DIR/$legacy_id"
      mv -- "$RELEASES_DIR/$legacy_id/.venv" "$INSTALL_DIR/.venv" || true
      rmdir -- "$RELEASES_DIR/$legacy_id" 2>/dev/null || true
      legacy_moved=false
    fi
    restore_profile_dropin_after_failure
    if [[ "$service_was_stopped" == true ]]; then
      systemctl start noyra >/dev/null 2>&1 || true
    fi
  elif [[ "$legacy_moved" == true ]]; then
    mkdir -p -- "$RELEASES_DIR/$legacy_id"
    mv -- "$RELEASES_DIR/$legacy_id/.venv" "$INSTALL_DIR/.venv" || true
    rmdir -- "$RELEASES_DIR/$legacy_id" 2>/dev/null || true
    legacy_moved=false
    restore_profile_dropin_after_failure
    if [[ "$service_was_stopped" == true ]]; then
      systemctl start noyra >/dev/null 2>&1 || true
    fi
  elif [[ "$service_was_stopped" == true ]]; then
    systemctl start noyra >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap on_error ERR

install -d -o root -g root -m 0755 "$INSTALL_DIR" "$RELEASES_DIR"
validate_native_config_paths
if [[ -L "$LOCK_FILE" || ( -e "$LOCK_FILE" && ! -f "$LOCK_FILE" ) ]]; then
  echo "Installer lock is not a regular file: $LOCK_FILE" >&2
  exit 1
fi
if [[ ! -e "$LOCK_FILE" ]]; then
  install -o root -g root -m 0600 /dev/null "$LOCK_FILE"
else
  chown root:root "$LOCK_FILE"
  chmod 0600 "$LOCK_FILE"
fi
exec 9>"$LOCK_FILE"
flock -n 9 || { echo 'Another Noyra installation or rollback is already running.' >&2; exit 1; }

if [[ "$rollback" == true ]]; then
  old_current=""
  old_previous=""
  validate_pointer_path "$CURRENT_LINK"
  validate_pointer_path "$PREVIOUS_LINK"
  if [[ -L "$CURRENT_LINK" ]]; then old_current="$(read_pointer "$CURRENT_LINK")"; fi
  if [[ -L "$PREVIOUS_LINK" ]]; then old_previous="$(read_pointer "$PREVIOUS_LINK")"; fi
  [[ -n "$old_current" && -n "$old_previous" ]] || { echo 'No usable current and previous releases are available.' >&2; exit 1; }
  [[ "$old_current" != "$old_previous" ]] || { echo 'Current and previous releases are identical; refusing rollback.' >&2; exit 1; }
  stop_old_service
  switched=true
  if [[ -n "$old_current" ]]; then
    atomic_pointer "$PREVIOUS_LINK" "$old_current"
  fi
  atomic_pointer "$CURRENT_LINK" "$old_previous"
  if [[ "$service_active" == true ]]; then
    if start_and_check; then
      :
    else
      echo 'Rollback selected the previous code but readiness failed; service remains stopped.' >&2
      on_error 1
    fi
  fi
  echo "Rolled back Noyra code to release: $old_previous"
  exit 0
fi

if [[ -z "$release_id" ]]; then
  commit="$(git -C "$SOURCE_DIR" rev-parse --verify --short=12 HEAD 2>/dev/null || true)"
  if [[ -n "$commit" ]]; then
    release_id="source-${commit}-$(date -u +%Y%m%d%H%M%S)"
  else
    release_id="source-$(date -u +%Y%m%d%H%M%S)-$$"
  fi
fi
assert_safe_segment "$release_id" release_id
release_root="$RELEASES_DIR/$release_id"
if [[ -e "$release_root" || -L "$release_root" ]]; then
  release_id="${release_id}-$$-$RANDOM"
  assert_safe_segment "$release_id" release_id
  release_root="$RELEASES_DIR/$release_id"
fi
[[ ! -e "$release_root" && ! -L "$release_root" ]] || { echo "Release already exists: $release_id" >&2; exit 1; }

old_current=""
old_previous=""
validate_pointer_path "$CURRENT_LINK"
validate_pointer_path "$PREVIOUS_LINK"
if [[ -L "$CURRENT_LINK" ]]; then old_current="$(read_pointer "$CURRENT_LINK")"; fi
if [[ -L "$PREVIOUS_LINK" ]]; then old_previous="$(read_pointer "$PREVIOUS_LINK")"; fi
[[ -n "$old_current" || -z "$old_previous" ]] || {
  echo 'Previous release exists without a current release; refusing installation.' >&2
  exit 1
}
if [[ -z "$old_current" && -d "$INSTALL_DIR/.venv" ]]; then
  [[ ! -L "$INSTALL_DIR/.venv" ]] || { echo 'Legacy .venv symlink is unsupported; migrate it manually.' >&2; exit 1; }
  legacy_venv="$INSTALL_DIR/.venv"
fi
configured_cloud_bucket="$(env_value NOYRA_ARCHIVE_S3_BUCKET '')"
if [[ "$profile" == base && -n "$configured_cloud_bucket" ]]; then
  echo 'S3 archive is configured; install with --profile cloud instead of base.' >&2
  exit 1
fi
if [[ -n "$old_current" || -n "$legacy_venv" ]]; then
  command -v runuser >/dev/null 2>&1 || {
    echo 'runuser is required before stopping Noyra for the encrypted upgrade backup.' >&2
    exit 1
  }
  assert_absolute_backup_dir
fi
stop_old_service

# A cold encrypted backup is mandatory before replacing an active release.
if [[ -n "$old_current" || -n "$legacy_venv" ]]; then
  backup_keyring="$BACKUP_KEYRING"
  backup_data_root="$(env_value NOYRA_DATA_DIR "$DATA_DIR")"
  backup_at_rest_mode="$(env_value NOYRA_AT_REST_MODE required)"
  backup_backend="$(env_value NOYRA_VOLUME_ENCRYPTION_BACKEND auto)"
  backup_attestation="$(env_value NOYRA_VOLUME_ATTESTATION_PATH '')"
  if [[ -L "$backup_dir" || ( -e "$backup_dir" && ! -d "$backup_dir" ) ]]; then
    echo "Backup directory must be a real directory: $backup_dir" >&2
    false
  fi
  assert_stable_backup_dir
  chown root:root -- "$backup_dir"
  chmod 0700 -- "$backup_dir"
  [[ "$(stat -c '%u:%g:%a' -- "$backup_dir" 2>/dev/null || true)" == '0:0:700' ]] || false
  backup_device="$(stat -c '%d' -- "$backup_dir")"
  backup_inode="$(stat -c '%i' -- "$backup_dir")"
  noyra_uid="$(id -u noyra)"
  noyra_gid="$(id -g noyra)"
  [[ "$noyra_uid" != 0 && "$noyra_gid" != 0 ]] || {
    echo 'The noyra service account must use non-root UID and GID values.' >&2
    false
  }
  # A hard kill cannot run the EXIT trap; remove only our root-level staging
  # names bearing our root-owned marker before opening a new service-writable
  # staging directory.
  assert_backup_dir_identity
  cleanup_stale_backup_staging
  assert_backup_dir_identity
  assert_stable_backup_dir
  # The service needs directory traversal only while writing the per-run
  # staging directory. It never receives list or write access to the archive
  # directory itself, and cleanup restores the root-only contract.
  backup_dir_exposed=true
  chown "root:$noyra_gid" "$backup_dir"
  chmod 0710 "$backup_dir"
  assert_backup_dir_identity
  backup_staging="$(mktemp -d "$backup_dir/.noyra-staging.XXXXXX")"
  printf '%s\n' 'noyra-upgrade-staging-v1' > "$backup_staging/.noyra-staging-marker"
  chown root:root "$backup_staging/.noyra-staging-marker"
  chmod 0600 "$backup_staging/.noyra-staging-marker"
  backup_lock_path="$backup_staging/.noyra-staging.lock"
  install -o root -g root -m 0600 /dev/null -- "$backup_lock_path"
  chown "root:$noyra_gid" "$backup_staging"
  # The backup process fsyncs its parent directory after publishing the
  # encrypted file, so the service group needs read/write/execute here.
  # Marker and lock files remain root-only and the directory is removed or
  # returned to 0700 before the installer continues.
  chmod 1770 "$backup_staging"
  assert_backup_dir_identity
  backup_label="${old_current:-legacy}"
  backup_path="$backup_dir/noyra-${backup_label}-$(date -u +%Y%m%dT%H%M%SZ).noyra-backup"
  staged_backup="$backup_staging/backup.noyra-backup"
  if [[ -n "$old_current" ]]; then
    backup_python="$RELEASES_DIR/$old_current/.venv/bin/python"
  else
    backup_python="$legacy_venv/bin/python"
  fi
  [[ -x "$backup_python" ]] || { echo "Current release has no usable Python." >&2; false; }
  [[ "$backup_data_root" = "$DATA_DIR" ]] || {
    echo 'NOYRA_DATA_DIR must remain /var/lib/noyra for the native Ubuntu installer.' >&2
    false
  }
  [[ "$backup_keyring" = "$BACKUP_KEYRING" ]] || {
    echo 'NOYRA_BACKUP_KEYRING_PATH must remain /etc/noyra/backup-keyring.json for the native Ubuntu installer.' >&2
    false
  }
  ensure_backup_keyring "$backup_python"
  backup_env=(NOYRA_DATA_DIR="$backup_data_root" NOYRA_AT_REST_MODE="$backup_at_rest_mode" NOYRA_VOLUME_ENCRYPTION_BACKEND="$backup_backend" NOYRA_VOLUME_ATTESTATION_PATH="$backup_attestation" NOYRA_BACKUP_KEYRING_PATH="$backup_keyring")
  flock --exclusive -- "$backup_lock_path" runuser --user=noyra --group=noyra -- /usr/bin/env "${backup_env[@]}" "$backup_python" -m noyra backup --output "$staged_backup"
  chown root:root "$backup_staging"
  chmod 0700 "$backup_staging"
  [[ -f "$staged_backup" && ! -L "$staged_backup" ]] || {
    echo 'Encrypted upgrade backup did not produce a regular file.' >&2
    false
  }
  staged_metadata="$(stat -c '%u:%g:%a:%h:%d' -- "$staged_backup" 2>/dev/null || true)"
  [[ "$staged_metadata" == "$noyra_uid:$noyra_gid:600:1:$backup_device" ]] || {
    echo 'Encrypted upgrade backup ownership or inode metadata is unsafe.' >&2
    false
  }
  [[ ! -e "$backup_path" && ! -L "$backup_path" ]] || {
    echo "Backup destination already exists: $backup_path" >&2
    false
  }
  chown root:root "$staged_backup"
  chmod 0600 "$staged_backup"
  mv -Tf -- "$staged_backup" "$backup_path"
  rm -f -- "$backup_lock_path"
  backup_lock_path=""
  rm -f -- "$backup_staging/.noyra-staging-marker"
  rmdir -- "$backup_staging"
  backup_staging=""
  restore_backup_dir
  backup_dir_exposed=false
  sync -d "$backup_dir" 2>/dev/null || sync 2>/dev/null || true
fi

staging="$RELEASES_DIR/.staging-${release_id}-$$-$RANDOM"
python3 -m venv "$staging/.venv"
"$staging/.venv/bin/python" -m pip install --require-hashes --requirement "$SOURCE_DIR/requirements.lock"
if [[ "$profile" == cloud ]]; then
  "$staging/.venv/bin/python" -m pip install --require-hashes --requirement "$SOURCE_DIR/requirements-cloud.lock"
fi
"$staging/.venv/bin/python" -m pip install --no-deps --no-build-isolation "$SOURCE_DIR"
"$staging/.venv/bin/python" -m pip check
"$staging/.venv/bin/python" - <<'PY'
import noyra
from noyra.service import ServiceSettings

assert noyra.__version__
assert ServiceSettings
PY
if [[ "$profile" == cloud ]]; then
  "$staging/.venv/bin/python" -c 'import boto3; assert boto3.__version__'
else
  if "$staging/.venv/bin/python" -c 'import boto3' >/dev/null 2>&1; then
    echo 'Base profile unexpectedly contains cloud dependency boto3.' >&2
    exit 1
  fi
fi

if [[ -n "$legacy_venv" ]]; then
  legacy_id="legacy-$(date -u +%Y%m%d%H%M%S)-$$"
  assert_safe_segment "$legacy_id" legacy_id
  mkdir -p -- "$RELEASES_DIR/$legacy_id"
  mv -- "$legacy_venv" "$RELEASES_DIR/$legacy_id/.venv"
  legacy_moved=true
  old_current="$legacy_id"
  legacy_venv=""
fi

mv -- "$staging" "$release_root"
staging=""
release_published=true
chown -R root:root "$release_root"
chmod 0755 "$release_root" "$release_root/.venv" "$release_root/.venv/bin"

ensure_backup_keyring "$release_root/.venv/bin/python"

install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/noyra.service" "$UNIT_FILE"
if [[ ! -f "$CONFIG_DIR/noyra.env" ]]; then
  install -o root -g noyra -m 0640 "$SOURCE_DIR/deploy/noyra.env.example" "$CONFIG_DIR/noyra.env"
fi
if [[ -L "$PROFILE_DROPIN_DIR" ]]; then
  echo "Profile drop-in directory cannot be a symlink: $PROFILE_DROPIN_DIR" >&2
  exit 1
fi
install -d -o root -g root -m 0755 "$PROFILE_DROPIN_DIR"
if [[ -L "$PROFILE_DROPIN" ]]; then
  echo "Profile drop-in cannot be a symlink: $PROFILE_DROPIN" >&2
  exit 1
fi
if [[ -e "$PROFILE_DROPIN" ]]; then
  [[ -f "$PROFILE_DROPIN" ]] || { echo "Profile drop-in is not a regular file: $PROFILE_DROPIN" >&2; exit 1; }
  cp -- "$PROFILE_DROPIN" "$profile_dropin_backup"
  chown root:root "$profile_dropin_backup"
  chmod 0644 "$profile_dropin_backup"
  profile_dropin_backup_exists=true
fi
profile_dropin_tmp="$PROFILE_DROPIN.tmp.$$.$RANDOM"
printf '[Service]\nEnvironment=NOYRA_INSTALL_PROFILE=%s\n' "$profile" > "$profile_dropin_tmp"
chown root:root "$profile_dropin_tmp"
chmod 0644 "$profile_dropin_tmp"
mv -Tf -- "$profile_dropin_tmp" "$PROFILE_DROPIN"
profile_dropin_changed=true

switched=true
if [[ -n "$old_current" ]]; then
  # Record the rollback target before publishing current.
  atomic_pointer "$PREVIOUS_LINK" "$old_current"
fi
atomic_pointer "$CURRENT_LINK" "$release_id"

systemctl daemon-reload
if [[ "$service_active" == true ]]; then
  if start_and_check; then
    :
  else
    echo 'New release failed readiness; restoring the previous code pointer.' >&2
    on_error 1
  fi
fi

echo "Installed Noyra profile: $profile"
echo "Release: $release_id"
if [[ -n "$old_current" ]]; then
  echo "Previous release: $old_current"
fi
if [[ -n "$backup_path" ]]; then
  echo "Cold backup: $backup_path"
fi
echo 'Edit /etc/noyra/noyra.env, then run: systemctl enable --now noyra'
rm -f -- "$profile_dropin_backup"
