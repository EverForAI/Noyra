#!/usr/bin/env bash

# Only metadata and installer-created empty state are rolled back. Unexpected
# runtime state is retained and blocks restarting an incompatible old release.
CONTROL_LAYOUT_CHANGED=false
CONTROL_LAYOUT_PATHS=()
CONTROL_LAYOUT_METADATA=()
declare -A CONTROL_LAYOUT_NEW_FILES=()

noyra_control_layout_snapshot() {
  local data_root="$1" path metadata
  shift
  CONTROL_LAYOUT_PATHS=("$@")
  CONTROL_LAYOUT_METADATA=()
  for path in "${CONTROL_LAYOUT_PATHS[@]}"; do
    [[ "$path" == "$data_root/"* && ! -L "$path" ]] || return 1
    if [[ -e "$path" ]]; then
      [[ -d "$path" || ( -f "$path" && "$(stat -c '%h' -- "$path")" == 1 ) ]] || return 1
      [[ "$(stat -c '%d' -- "$path")" == "$(stat -c '%d' -- "$data_root")" ]] || return 1
      metadata="$(stat -c '%u:%g:%a' -- "$path")" || return 1
    else
      metadata=absent
    fi
    CONTROL_LAYOUT_METADATA+=("$metadata")
  done
  CONTROL_LAYOUT_CHANGED=true
}

noyra_control_layout_record_file() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" && "$(stat -c '%h' -- "$path")" == 1 ]] || return 1
  CONTROL_LAYOUT_NEW_FILES["$path"]="$(stat -c '%d:%i' -- "$path"):$(sha256sum -- "$path" | cut -d' ' -f1)"
}

noyra_control_layout_restore() {
  [[ "$CONTROL_LAYOUT_CHANGED" == true ]] || return 0
  local index path metadata current failed=false
  for ((index=${#CONTROL_LAYOUT_PATHS[@]}-1; index>=0; index--)); do
    path="${CONTROL_LAYOUT_PATHS[index]}"
    metadata="${CONTROL_LAYOUT_METADATA[index]}"
    if [[ -L "$path" ]]; then
      failed=true
    elif [[ "$metadata" != absent ]]; then
      if [[ ! -e "$path" ]]; then
        failed=true
        continue
      fi
      chown "${metadata%:*}" -- "$path" && chmod "${metadata##*:}" -- "$path" || failed=true
    elif [[ -d "$path" ]]; then
      rmdir -- "$path" || failed=true
    elif [[ -e "$path" ]]; then
      [[ -f "$path" && "$(stat -c '%h' -- "$path")" == 1 ]] || { failed=true; continue; }
      current="$(stat -c '%d:%i' -- "$path"):$(sha256sum -- "$path" | cut -d' ' -f1)"
      if [[ -n "${CONTROL_LAYOUT_NEW_FILES[$path]:-}" && "$current" == "${CONTROL_LAYOUT_NEW_FILES[$path]}" ]]; then
        rm -f -- "$path" || failed=true
      else
        failed=true
      fi
    fi
  done
  [[ "$failed" == false ]] || return 1
  CONTROL_LAYOUT_CHANGED=false
}
