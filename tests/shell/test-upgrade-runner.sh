#!/usr/bin/env bash
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
runner="$root/scripts/upgrade-ubuntu-runner.sh"
[[ -f "$runner" ]] || fail 'upgrade runner is missing'

test_root="$(mktemp -d "${TMPDIR:-/tmp}/noyra-upgrade-runner.XXXXXX")"
cleanup() { rm -rf --one-file-system -- "$test_root"; }
trap cleanup EXIT

export NOYRA_UPGRADE_TEST_MODE=1
export NOYRA_UPGRADE_TEST_ROOT="$test_root"
export NOYRA_UPGRADE_TEST_REMOTE_URL="$test_root/origin.git"

git init --bare -q "$NOYRA_UPGRADE_TEST_REMOTE_URL"
work="$test_root/work"
git init -q "$work"
git -C "$work" config user.email 'noyra-test@example.invalid'
git -C "$work" config user.name 'Noyra runner test'
printf 'safe\n' > "$work/source.txt"
mkdir -p "$work/scripts"
cat > "$work/scripts/install-ubuntu.sh" <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == --profile && "$2" == base ]]
[[ "$3" == --release-id && "$4" =~ ^github-[0-9a-f]{7}-[0-9]{14}$ ]]
printf '%s\n' "$4" >> "$NOYRA_UPGRADE_TEST_ROOT/installer.called"
if [[ "${NOYRA_UPGRADE_TEST_INSTALL_DELAY:-0}" == 1 ]]; then sleep 2; fi
mkdir -p "$NOYRA_UPGRADE_TEST_ROOT/opt/noyra/releases/$4"
printf '%s\n' "$NOYRA_UPGRADE_TEST_TARGET_SHA" > \
  "$NOYRA_UPGRADE_TEST_ROOT/opt/noyra/releases/$4/.noyra-source-sha"
ln -sfn "releases/$4" "$NOYRA_UPGRADE_TEST_ROOT/opt/noyra/current"
INSTALLER
chmod 0755 "$work/scripts/install-ubuntu.sh"
git -C "$work" add source.txt scripts/install-ubuntu.sh
git -C "$work" commit -qm 'safe update'
target_sha="$(git -C "$work" rev-parse HEAD)"
git -C "$work" branch -M main
git -C "$work" remote add origin "$NOYRA_UPGRADE_TEST_REMOTE_URL"
git -C "$work" push -q -u origin main

source="$test_root/source"
mkdir -p "$test_root/bin"

cat > "$test_root/bin/curl" <<'CURL'
#!/usr/bin/env bash
echo 200
CURL
chmod 0755 "$test_root/bin/curl"
export PATH="$test_root/bin:$PATH"
export NOYRA_UPGRADE_TEST_TARGET_SHA="$target_sha"

request_dir="$test_root/state/requests"
mkdir -p "$request_dir"
handoff_lock="$test_root/state/manager.lock"
: > "$handoff_lock"
write_request() {
  local sha="$1" task="$2"
  printf '{"task_id":"%s","target_sha":"%s","requested_at":"2026-10-01T00:00:00Z"}\n' \
    "$task" "$sha" > "$request_dir/pending.json"
}

write_request "$target_sha" 'task-valid-0001'
export NOYRA_UPGRADE_TEST_INSTALL_DELAY=1
exec 8>"$handoff_lock"
flock -x 8
bash "$runner" &
runner_pid=$!
sleep 0.25
if [[ ! -f "$request_dir/pending.json" ]]; then
  flock -u 8
  wait "$runner_pid" || true
  fail 'runner consumed a request while the manager handoff lock was held'
fi
flock -u 8
exec 8>&-
for _ in $(seq 1 40); do
  [[ -f "$test_root/state/status.json" ]] && break
  sleep 0.05
done
[[ -f "$test_root/state/status.json" ]] || fail 'runner did not publish initial status'
status="$(cat "$test_root/state/status.json")"
[[ "$status" == *'"status":"running"'* ]] || fail 'runner did not expose running state'
# A duplicate activation while the installer runs must not start a second one.
bash "$runner"
status="$(cat "$test_root/state/status.json")"
[[ "$status" == *'"status":"running"'* ]] || fail 'duplicate activation changed the running status'
wait "$runner_pid"
status="$(cat "$test_root/state/status.json")"
[[ "$status" == *'"status":"completed"'* ]] || fail 'runner did not complete status'
[[ ! -e "$request_dir/pending.json" ]] || fail 'runner did not consume the request'
[[ -f "$test_root/installer.called" ]] || fail 'runner did not invoke the fixed installer'
[[ "$(wc -l < "$test_root/installer.called")" -eq 1 ]] || fail 'runner started more than one installer'
[[ "$(stat -c '%a' "$source/.git")" == 755 ]] || \
  fail 'root-owned source metadata is not readable by the application account'
[[ "$(stat -c '%a' "$source/.git/config")" == 644 ]] || \
  fail 'root-owned source metadata files are not readable by the application account'

# SHA validation happens before Git or the installer; metacharacters remain data.
rm -f "$test_root/installer.called"
injection_marker="$test_root/injection-ran"
write_request "\$(touch $injection_marker)" 'task-malicious-01'
if bash "$runner"; then fail 'runner accepted an invalid SHA'; fi
[[ ! -e "$injection_marker" ]] || fail 'request text was evaluated by a shell'
[[ ! -e "$test_root/installer.called" ]] || fail 'invalid SHA reached installer'
status="$(cat "$test_root/state/status.json")"
[[ "$status" == *'"status":"failed"'* ]] || fail 'invalid request did not publish failed state'
[[ "$status" == *'"error_code":"upgrade_target_invalid"'* ]] || \
  fail 'invalid request exposed an unstable error code'

# The root runner accepts only the official repository and is installed as a
# fixed systemd path/service pair without inheriting the application's secrets.
path_unit="$root/deploy/systemd/noyra-upgrade.path"
service_unit="$root/deploy/systemd/noyra-upgrade.service"
recover_unit="$root/deploy/systemd/noyra-upgrade-recover.service"
[[ -f "$path_unit" && -f "$service_unit" && -f "$recover_unit" ]] || \
  fail 'systemd trigger and recovery units are missing'
grep -Fq 'PathExists=/var/lib/noyra/upgrade/requests/pending.json' "$path_unit" || \
  fail 'systemd path trigger is not fixed to the pending request'
grep -Fq 'ExecStart=/usr/local/libexec/noyra-upgrade-runner.sh' "$service_unit" || \
  fail 'systemd runner executable is not fixed'
! grep -Eq 'EnvironmentFile=.*noyra\.env' "$service_unit" || \
  fail 'privileged runner inherits Noyra secrets'
grep -Fq 'ExecStart=/usr/local/libexec/noyra-upgrade-runner.sh --recover-only' "$recover_unit" || \
  fail 'boot recovery is not wired to the fixed runner'

# On boot, a persisted active task becomes a stable interrupted status.
printf '{"task_id":"task-reboot-0001","status":"running","target_sha":"%s","started_at":"2026-10-01T00:00:00Z"}\n' \
  "$target_sha" > "$test_root/state/status.json"
bash "$runner" --recover-only
status="$(cat "$test_root/state/status.json")"
[[ "$status" == *'"status":"interrupted"'* ]] || fail 'boot recovery did not mark the interrupted task'
[[ "$status" == *'"error_code":"upgrade_interrupted"'* ]] || \
  fail 'boot recovery did not use the stable interrupted code'

echo 'upgrade runner contract checks passed'
