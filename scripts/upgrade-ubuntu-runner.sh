#!/usr/bin/env bash
set -Eeuo pipefail
umask 0027

# This runner is installed root-owned and is activated by a fixed systemd path
# unit. The only request data accepted from Noyra is a task id and a commit SHA.

TEST_MODE=false
if [[ "${NOYRA_UPGRADE_TEST_MODE:-}" == 1 ]]; then
  TEST_MODE=true
  test_root="${NOYRA_UPGRADE_TEST_ROOT:?}"
  [[ "$test_root" == /* && -d "$test_root" ]] || exit 2
  INSTALL_ROOT="$test_root/opt/noyra"
  SOURCE_DIR="$test_root/source"
  UPGRADE_ROOT="$test_root/state"
  REQUEST_DIR="$UPGRADE_ROOT/requests"
  PROCESSING_DIR="$UPGRADE_ROOT/processing"
  REQUEST_PATH="$REQUEST_DIR/pending.json"
  STATUS_PATH="$UPGRADE_ROOT/status.json"
  HANDOFF_LOCK_PATH="$UPGRADE_ROOT/manager.lock"
  REMOTE_URL="${NOYRA_UPGRADE_TEST_REMOTE_URL:?}"
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
  [[ -n "$PYTHON_BIN" ]] || exit 2
else
  INSTALL_ROOT=/opt/noyra
  SOURCE_DIR=/opt/noyra/upgrade/source
  UPGRADE_ROOT=/var/lib/noyra/upgrade
  REQUEST_DIR="$UPGRADE_ROOT/requests"
  PROCESSING_DIR="$UPGRADE_ROOT/processing"
  REQUEST_PATH="$REQUEST_DIR/pending.json"
  STATUS_PATH="$UPGRADE_ROOT/status.json"
  HANDOFF_LOCK_PATH="$UPGRADE_ROOT/manager.lock"
  REMOTE_URL=https://github.com/EverForAI/Noyra.git
  PYTHON_BIN=/usr/bin/python3
  [[ ${EUID:-$(id -u)} -eq 0 ]] || exit 1
fi

readonly GITHUB_BRANCH=main
readonly RUNNER_LOCK="$INSTALL_ROOT/.upgrade-runner.lock"
task_id=""
target_sha=""
started_at=""
release_id=""
status_written=false
error_code=upgrade_start_failed
phase=starting
processing_file=""

utc_now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

write_status() {
  local status="$1" next_phase="$2" code="${3:-}" ended="${4:-}"
  local log1="${5:-}" log2="${6:-}" log3="${7:-}"
  local group_args=()
  if [[ "$TEST_MODE" != true ]]; then
    group_args=("$(getent group noyra | cut -d: -f3)")
    [[ -n "${group_args[0]}" ]] || return 1
  fi
  "$PYTHON_BIN" - "$STATUS_PATH" "$task_id" "$status" "$next_phase" \
    "$started_at" "$ended" "$target_sha" "$target_sha" "$code" \
    "$log1" "$log2" "$log3" "${group_args[0]:-}" <<'PY'
import json
import os
import secrets
import sys

(path, task_id, status, phase, started_at, ended_at, target_sha, release,
 error_code, log1, log2, log3, group_id) = sys.argv[1:]
directory = os.path.dirname(path)
if os.path.islink(directory) or not os.path.isdir(directory):
    raise SystemExit(1)
payload = {
    "task_id": task_id or None,
    "status": status,
    "phase": phase,
    "started_at": started_at or None,
    "ended_at": ended_at or None,
    "target_sha": target_sha or None,
    "release": release or None,
    "error_code": error_code or None,
    "logs": [line for line in (log1, log2, log3) if line],
}
temporary = os.path.join(directory, ".status-" + secrets.token_hex(12) + ".tmp")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(temporary, flags, 0o640)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if group_id:
        os.chown(temporary, 0, int(group_id))
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
  status_written=true
}

finish_failed() {
  local rc=$?
  if [[ "$status_written" == true && -n "$task_id" && "$phase" != completed && "$phase" != failed ]]; then
    write_status failed failed "$error_code" "$(utc_now)" \
      '升级未完成；旧版本由安装器保护。' || true
  fi
  if [[ -n "$processing_file" ]]; then rm -f -- "$processing_file"; fi
  return "$rc"
}
trap finish_failed EXIT

if [[ "$TEST_MODE" == true ]]; then
  mkdir -p -- "$INSTALL_ROOT" "$PROCESSING_DIR"
fi
if [[ "$TEST_MODE" != true ]]; then
  for path in /opt /var/lib/noyra "$INSTALL_ROOT" "$UPGRADE_ROOT" "$REQUEST_DIR" "$PROCESSING_DIR"; do
    [[ ! -L "$path" && -d "$path" ]] || exit 1
  done
  [[ "$(stat -c '%u:%g:%a' -- "$INSTALL_ROOT")" == '0:0:755' ]] || exit 1
  [[ "$(stat -c '%u:%g:%a' -- /opt/noyra/upgrade)" == '0:0:755' ]] || exit 1
  [[ "$(stat -c '%u:%g:%a' -- "$UPGRADE_ROOT")" == "0:$(getent group noyra | cut -d: -f3):750" ]] || exit 1
  [[ "$(stat -c '%u:%g:%a' -- "$REQUEST_DIR")" == "$(id -u noyra):$(id -g noyra):700" ]] || exit 1
  [[ "$(stat -c '%u:%g:%a' -- "$PROCESSING_DIR")" == '0:0:700' ]] || exit 1
  [[ "$(stat -c '%u:%g:%a' -- "$HANDOFF_LOCK_PATH" 2>/dev/null || true)" == \
    "0:$(getent group noyra | cut -d: -f3):660" ]] || exit 1
fi
[[ ! -L "$INSTALL_ROOT" && -d "$INSTALL_ROOT" ]] || exit 1
[[ ! -L "$UPGRADE_ROOT" && -d "$UPGRADE_ROOT" ]] || exit 1
[[ ! -L "$REQUEST_DIR" && -d "$REQUEST_DIR" ]] || exit 1
[[ ! -L "$PROCESSING_DIR" && -d "$PROCESSING_DIR" ]] || exit 1

[[ ! -L "$RUNNER_LOCK" && ( ! -e "$RUNNER_LOCK" || -f "$RUNNER_LOCK" ) ]] || exit 1
exec 9>"$RUNNER_LOCK"
flock -n 9 || exit 0

if [[ "${1:-}" == --recover-only ]]; then
  [[ $# -eq 1 ]] || exit 2
  if [[ -f "$STATUS_PATH" && ! -L "$STATUS_PATH" ]]; then
    set +e
    "$PYTHON_BIN" - "$STATUS_PATH" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as stream:
        value = json.load(stream)
except (OSError, ValueError):
    raise SystemExit(0)
if isinstance(value, dict) and value.get("status") in {"queued", "running", "rolling_back"}:
    raise SystemExit(10)
raise SystemExit(0)
PY
    recovery_rc=$?
    set -e
    if [[ "$recovery_rc" -eq 10 ]]; then
      task_id="$("$PYTHON_BIN" - "$STATUS_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
item = value.get("task_id", "")
print(item if isinstance(item, str) and len(item) <= 64 else "")
PY
)"
      target_sha="$("$PYTHON_BIN" - "$STATUS_PATH" <<'PY'
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
item = value.get("target_sha", "")
print(item if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40,64}", item) else "")
PY
)"
      started_at="$(utc_now)"
      write_status interrupted interrupted upgrade_interrupted "$(utc_now)" \
        '升级进程在系统重启或中断后未完成；请重新检查版本并发起升级。'
    elif [[ "$recovery_rc" -ne 0 ]]; then
      exit "$recovery_rc"
    fi
  fi
  trap - EXIT
  exit 0
fi
[[ $# -eq 0 ]] || exit 2

# Serialize the rename and first status write with the application's manager
# lock. Otherwise a concurrent POST can observe neither pending.json nor an
# active status and enqueue a second root installation.
[[ ! -L "$HANDOFF_LOCK_PATH" && -f "$HANDOFF_LOCK_PATH" ]] || exit 1
exec 8>>"$HANDOFF_LOCK_PATH"
flock -x 8

# Move the untrusted directory entry into a root-only directory before opening
# it. Rename never follows a symlink; the parser below also uses O_NOFOLLOW.
if [[ ! -e "$REQUEST_PATH" && ! -L "$REQUEST_PATH" ]]; then
  trap - EXIT
  exit 0
fi
processing_file="$PROCESSING_DIR/request-$(date -u +%s)-$$.json"
[[ ! -e "$processing_file" && ! -L "$processing_file" ]] || exit 1
mv -T -- "$REQUEST_PATH" "$processing_file"

set +e
parsed="$("$PYTHON_BIN" - "$processing_file" <<'PY'
import json
import os
import re
import stat
import sys
from datetime import datetime

path = sys.argv[1]
try:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise ValueError
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    request = json.loads(raw, object_pairs_hook=unique_pairs)
    if not isinstance(request, dict) or set(request) != {"task_id", "target_sha", "requested_at"}:
        raise ValueError
    task = request["task_id"]
    sha = request["target_sha"]
    requested_at = request["requested_at"]
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", task):
        raise ValueError
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        print(task, "", "", sep="\t")
        raise SystemExit(1)
    if not isinstance(requested_at, str) or len(requested_at) > 40:
        raise ValueError
    datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    print(task, sha, requested_at, sep="\t")
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    print("\t\t")
    raise SystemExit(1)
PY
)"
parse_rc=$?
set -e
IFS=$'\t' read -r task_id target_sha started_at <<< "$parsed"
[[ "$task_id" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,63}$ ]] || task_id=""
[[ "$target_sha" =~ ^[0-9a-f]{40,64}$ ]] || target_sha=""
if [[ "$parse_rc" -ne 0 ]]; then
  error_code=upgrade_target_invalid
  phase=failed
  write_status failed failed "$error_code" "$(utc_now)" \
    '升级请求无效，未执行安装。'
  flock -u 8
  rm -f -- "$processing_file"
  trap - EXIT
  exit 1
fi

started_at="$(utc_now)"
phase=fetching
write_status running "$phase" "" "" '已安全接收升级请求。' || exit 1
flock -u 8

# Use only the installed verifier before executing newly fetched code.
channel=development-main
fetch_ref="$GITHUB_BRANCH"
if [[ "$TEST_MODE" != true ]]; then
  trusted_python="$INSTALL_ROOT/current/.venv/bin/python"
  [[ -x "$trusted_python" ]] || { error_code=upgrade_unavailable; exit 1; }
  channel="$("$trusted_python" -I -m noyra.core.release_assurance --channel)" || {
    error_code=upgrade_release_unverified; exit 1;
  }
  if [[ "$channel" == stable ]]; then
    evidence_dir="$INSTALL_ROOT/upgrade/verified/$target_sha"
    [[ ! -L "$INSTALL_ROOT/upgrade/verified" && ! -L "$evidence_dir" ]] || exit 1
    install -d -o root -g root -m 0755 "$INSTALL_ROOT/upgrade/verified" "$evidence_dir"
    evidence_file="$evidence_dir/external-gates.json"
    [[ ! -L "$evidence_file" && ( ! -e "$evidence_file" || -f "$evidence_file" ) ]] || exit 1
    rm -f -- "$evidence_file"
    fetch_ref="$("$trusted_python" -I -m noyra.core.release_assurance \
      --verify-target "$target_sha" --output "$evidence_file")" || {
      error_code=upgrade_release_unverified; exit 1;
    }
    [[ "$fetch_ref" =~ ^v[0-9][A-Za-z0-9._-]{0,99}$ ]] || exit 1
  elif [[ "$channel" != development-main ]]; then
    error_code=upgrade_release_unverified; exit 1
  fi
fi

if [[ "$TEST_MODE" == true ]]; then
  if [[ ! -e "$SOURCE_DIR" && ! -L "$SOURCE_DIR" ]]; then
    git clone --quiet --branch "$GITHUB_BRANCH" --single-branch "$REMOTE_URL" "$SOURCE_DIR" \
      >/dev/null 2>&1 || { error_code=upgrade_unavailable; exit 1; }
  fi
else
  for path in /opt/noyra/upgrade /opt/noyra/upgrade/source /var/lib/noyra/upgrade; do
    [[ ! -L "$path" ]] || { error_code=upgrade_unavailable; exit 1; }
  done
  source_owner="$(stat -c '%u:%g:%a' -- "$SOURCE_DIR" 2>/dev/null || true)"
  if [[ ! -e "$SOURCE_DIR" ]]; then
    install -d -o root -g root -m 0755 /opt/noyra/upgrade
    git clone --quiet --branch "$GITHUB_BRANCH" --single-branch "$REMOTE_URL" "$SOURCE_DIR" \
      >/dev/null 2>&1 || { error_code=upgrade_unavailable; exit 1; }
    chown -R root:root "$SOURCE_DIR"
    chmod 0755 "$SOURCE_DIR"
  elif [[ "$source_owner" != '0:0:755' ]]; then
    error_code=upgrade_unavailable
    exit 1
  fi
fi

[[ -d "$SOURCE_DIR" && ! -L "$SOURCE_DIR" ]] || { error_code=upgrade_unavailable; exit 1; }
chmod -R a+rX "$SOURCE_DIR" || { error_code=upgrade_unavailable; exit 1; }
if [[ "$TEST_MODE" != true ]]; then
  [[ -d "$SOURCE_DIR/.git" && ! -L "$SOURCE_DIR/.git" ]] || {
    error_code=upgrade_unavailable
    exit 1
  }
  unsafe_source_entry="$(find "$SOURCE_DIR" -xdev \( ! -uid 0 -o -perm /0022 \) -print -quit 2>/dev/null || true)"
  [[ -z "$unsafe_source_entry" ]] || { error_code=upgrade_unavailable; exit 1; }
fi
git -C "$SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  error_code=upgrade_unavailable
  exit 1
}
remote_url="$(git -C "$SOURCE_DIR" remote get-url origin 2>/dev/null || true)"
[[ "$remote_url" == "$REMOTE_URL" ]] || { error_code=upgrade_unavailable; exit 1; }
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all 2>/dev/null || true)" ]] || {
  error_code=upgrade_source_dirty
  exit 1
}
git -C "$SOURCE_DIR" fetch --quiet --depth=1 origin "$fetch_ref" \
  >/dev/null 2>&1 || { error_code=upgrade_unavailable; exit 1; }
latest_sha="$(git -C "$SOURCE_DIR" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
[[ "$latest_sha" =~ ^[0-9a-f]{40,64}$ && "$target_sha" == "$latest_sha" ]] || {
  error_code=upgrade_target_stale
  exit 1
}
git -C "$SOURCE_DIR" checkout --quiet --detach "$target_sha" >/dev/null 2>&1 || {
  error_code=upgrade_unavailable
  exit 1
}
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all 2>/dev/null || true)" ]] || {
  error_code=upgrade_source_dirty
  exit 1
}
chmod -R a+rX "$SOURCE_DIR" || { error_code=upgrade_unavailable; exit 1; }

profile=base
if [[ "$TEST_MODE" != true ]]; then
  profile_file=/etc/systemd/system/noyra.service.d/profile.conf
  if [[ -e "$profile_file" || -L "$profile_file" ]]; then
    [[ -f "$profile_file" && ! -L "$profile_file" ]] || {
      error_code=upgrade_unavailable
      exit 1
    }
    profile_value="$(sed -n 's/^Environment=NOYRA_INSTALL_PROFILE=\(base\|cloud\)$/\1/p' "$profile_file")"
    [[ "$profile_value" =~ ^(base|cloud)$ ]] || { error_code=upgrade_unavailable; exit 1; }
    profile="$profile_value"
  fi
fi

release_id="github-${target_sha:0:7}-$(date -u +%Y%m%d%H%M%S)"
[[ "$release_id" =~ ^github-[0-9a-f]{7}-[0-9]{14}$ ]] || {
  error_code=upgrade_start_failed
  exit 1
}
phase=installing
write_status running "$phase" "" "" \
  "已验证官方版本；升级通道：$channel。" '正在安装并执行健康检查。' || exit 1
if ! bash "$SOURCE_DIR/scripts/install-ubuntu.sh" \
  --profile "$profile" --release-id "$release_id" >/dev/null 2>&1; then
  error_code=upgrade_install_failed
  exit 1
fi

if [[ "$TEST_MODE" == true ]]; then
  installed_sha="$target_sha"
else
  current_release="$(readlink -f /opt/noyra/current 2>/dev/null || true)"
  [[ "$current_release" == /opt/noyra/releases/* ]] || {
    error_code=upgrade_install_failed
    exit 1
  }
  installed_sha="$(cat "$current_release/.noyra-source-sha" 2>/dev/null || true)"
fi
[[ "$installed_sha" == "$target_sha" ]] || { error_code=upgrade_install_failed; exit 1; }
target_sha="$installed_sha"
phase=completed
write_status completed completed "" "$(utc_now)" \
  '升级完成；新版本已通过安装器健康检查。' || exit 1
rm -f -- "$processing_file"
trap - EXIT
exit 0
