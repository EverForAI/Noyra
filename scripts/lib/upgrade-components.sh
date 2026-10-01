#!/usr/bin/env bash

# Functions shared by the Ubuntu installer and its shell contract tests. The
# caller must snapshot before changing any root-owned upgrade component and
# restore them from its ERR handler until the release has passed readiness.

UPGRADE_COMPONENTS_BACKUP_DIR=""
UPGRADE_COMPONENTS_CHANGED=false
UPGRADE_PATH_WAS_ENABLED=false
UPGRADE_PATH_WAS_ACTIVE=false
UPGRADE_RECOVER_WAS_ENABLED=false

noyra_upgrade_components_snapshot() {
  if [[ $# -ne 5 ]]; then
    echo 'Expected backup directory, runner and three unit paths.' >&2
    return 2
  fi
  local backup_dir="$1"
  shift
  local -a paths=("$@")
  local index=0 path state

  UPGRADE_PATH_WAS_ENABLED=false
  UPGRADE_PATH_WAS_ACTIVE=false
  UPGRADE_RECOVER_WAS_ENABLED=false

  [[ ! -e "$backup_dir" && ! -L "$backup_dir" ]] || {
    echo 'Upgrade component snapshot path already exists.' >&2
    return 1
  }
  mkdir -m 0700 -- "$backup_dir" || return 1
  for path in "${paths[@]}"; do
    if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
      rm -rf -- "$backup_dir"
      echo "Upgrade component must be a regular file: $path" >&2
      return 1
    fi
    if [[ -f "$path" ]]; then
      cp -a -- "$path" "$backup_dir/component-$index" || {
        rm -rf -- "$backup_dir"
        return 1
      }
      : > "$backup_dir/present-$index" || {
        rm -rf -- "$backup_dir"
        return 1
      }
    fi
    index=$((index + 1))
  done

  state="$(systemctl is-enabled noyra-upgrade.path 2>/dev/null || true)"
  case "$state" in enabled|enabled-runtime|linked|linked-runtime|alias) UPGRADE_PATH_WAS_ENABLED=true ;; esac
  state="$(systemctl is-active noyra-upgrade.path 2>/dev/null || true)"
  [[ "$state" == active ]] && UPGRADE_PATH_WAS_ACTIVE=true
  state="$(systemctl is-enabled noyra-upgrade-recover.service 2>/dev/null || true)"
  case "$state" in enabled|enabled-runtime|linked|linked-runtime|alias) UPGRADE_RECOVER_WAS_ENABLED=true ;; esac

  UPGRADE_COMPONENTS_BACKUP_DIR="$backup_dir"
  UPGRADE_COMPONENTS_CHANGED=false
}

noyra_upgrade_components_mark_changed() {
  [[ -n "$UPGRADE_COMPONENTS_BACKUP_DIR" && -d "$UPGRADE_COMPONENTS_BACKUP_DIR" ]] || {
    echo 'Upgrade component snapshot is missing.' >&2
    return 1
  }
  UPGRADE_COMPONENTS_CHANGED=true
}

noyra_upgrade_components_restore() {
  [[ "$UPGRADE_COMPONENTS_CHANGED" == true ]] || return 0
  [[ $# -eq 4 ]] || return 2
  local runner_path="$1" path_unit_path="$2" service_unit_path="$3" recover_unit_path="$4"
  local -a paths=("$runner_path" "$path_unit_path" "$service_unit_path" "$recover_unit_path")
  local index=0 path temporary state

  state="$(systemctl is-active noyra-upgrade.path 2>/dev/null || true)"
  case "$state" in
    active|activating|deactivating|reloading)
      systemctl stop noyra-upgrade.path >/dev/null 2>&1 || return 1
      ;;
  esac
  for path in "${paths[@]}"; do
    if [[ -e "$UPGRADE_COMPONENTS_BACKUP_DIR/present-$index" ]]; then
      temporary="${path}.rollback.$$"
      cp -a -- "$UPGRADE_COMPONENTS_BACKUP_DIR/component-$index" "$temporary" || return 1
      mv -Tf -- "$temporary" "$path" || return 1
    else
      rm -f -- "$path" || return 1
    fi
    index=$((index + 1))
  done

  systemctl daemon-reload >/dev/null 2>&1 || return 1
  if [[ "$UPGRADE_RECOVER_WAS_ENABLED" == true ]]; then
    systemctl enable noyra-upgrade-recover.service >/dev/null 2>&1 || return 1
  else
    systemctl disable noyra-upgrade-recover.service >/dev/null 2>&1 || true
  fi
  if [[ "$UPGRADE_PATH_WAS_ENABLED" == true ]]; then
    if [[ "$UPGRADE_PATH_WAS_ACTIVE" == true ]]; then
      systemctl enable --now noyra-upgrade.path >/dev/null 2>&1 || return 1
    else
      systemctl enable noyra-upgrade.path >/dev/null 2>&1 || return 1
      systemctl stop noyra-upgrade.path >/dev/null 2>&1 || return 1
    fi
  else
    systemctl disable --now noyra-upgrade.path >/dev/null 2>&1 || true
  fi

  rm -rf -- "$UPGRADE_COMPONENTS_BACKUP_DIR" || return 1
  UPGRADE_COMPONENTS_BACKUP_DIR=""
  UPGRADE_COMPONENTS_CHANGED=false
}

noyra_upgrade_components_commit() {
  [[ "$UPGRADE_COMPONENTS_CHANGED" == true ]] || return 0
  rm -rf -- "$UPGRADE_COMPONENTS_BACKUP_DIR" || return 1
  UPGRADE_COMPONENTS_BACKUP_DIR=""
  UPGRADE_COMPONENTS_CHANGED=false
}
