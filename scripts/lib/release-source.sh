#!/usr/bin/env bash

# Build inputs, including ignored files, must be exactly the named commit.
# No mutable checkout files are consumed after this snapshot is created.
noyra_snapshot_release_source() {
  local source_dir="$1" source_sha="$2" destination="$3" status
  [[ "$source_sha" =~ ^[0-9a-f]{40,64}$ ]] || return 1
  [[ -d "$destination" && ! -L "$destination" ]] || return 1
  [[ -z "$(find "$destination" -mindepth 1 -print -quit)" ]] || return 1
  [[ "$(git -C "$source_dir" rev-parse --verify 'HEAD^{commit}')" == "$source_sha" ]] || return 1
  status="$(git -C "$source_dir" status --porcelain --untracked-files=all)" || return 1
  if [[ -n "$status" ]]; then
    echo 'Release source is dirty; commit or remove changes before installing.' >&2
    return 1
  fi
  # Consume all ls-tree output: grep -q can induce SIGPIPE in git under
  # pipefail and accidentally turn a positive submodule check into failure.
  if git -C "$source_dir" ls-tree -r "$source_sha" | grep '^160000 ' >/dev/null; then
    echo 'Release source contains unsupported submodules.' >&2
    return 1
  fi
  git -C "$source_dir" archive --format=tar "$source_sha" |
    tar -xf - --no-same-owner -C "$destination" || return 1
  if [[ -n "$(find "$destination" -type l -print -quit)" ]]; then
    echo 'Release source symlinks are not supported by the immutable build boundary.' >&2
    return 1
  fi
}
