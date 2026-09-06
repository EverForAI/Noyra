#!/usr/bin/env bash
set -euo pipefail

# Read-only service validation and evidence collection for a disposable Ubuntu
# host. This script never stops, restarts, kills, reconfigures, or fills a host.

usage() {
  cat <<'EOF'
Usage:
  remote-acceptance.sh preflight [evidence-dir]
  remote-acceptance.sh smoke [evidence-dir]
  remote-acceptance.sh soak <duration-seconds> [evidence-dir]
  remote-acceptance.sh collect [evidence-dir]

Environment:
  NOYRA_SERVICE_NAME  systemd unit, default: noyra
  NOYRA_HEALTH_URL    local health base URL, default: http://127.0.0.1:8765
  NOYRA_SOAK_INTERVAL_SECONDS  probe interval, default: 30
EOF
}

mode="${1:-}"
case "$mode" in
  preflight|smoke|collect) ;;
  soak)
    [[ $# -ge 2 && "$2" =~ ^[0-9]+$ && "$2" -gt 0 ]] || {
      echo 'soak requires a positive integer duration in seconds.' >&2
      usage >&2
      exit 2
    }
    ;;
  *) usage >&2; exit 2 ;;
esac

if [[ "$mode" == soak ]]; then
  duration_seconds="$2"
  evidence_dir="${3:-${NOYRA_EVIDENCE_DIR:-/var/tmp/noyra-acceptance-$(date -u +%Y%m%dT%H%M%SZ)}}"
else
  evidence_dir="${2:-${NOYRA_EVIDENCE_DIR:-/var/tmp/noyra-acceptance-$(date -u +%Y%m%dT%H%M%SZ)}}"
fi

service_name="${NOYRA_SERVICE_NAME:-noyra}"
health_url="${NOYRA_HEALTH_URL:-http://127.0.0.1:8765}"
interval_seconds="${NOYRA_SOAK_INTERVAL_SECONDS:-30}"
[[ "$interval_seconds" =~ ^[0-9]+$ && "$interval_seconds" -gt 0 ]] || {
  echo 'NOYRA_SOAK_INTERVAL_SECONDS must be a positive integer.' >&2
  exit 2
}

umask 077
mkdir -p -- "$evidence_dir"
evidence_dir="$(cd -- "$evidence_dir" && pwd)"
log_file="$evidence_dir/remote-acceptance.log"
exec > >(tee -a "$log_file") 2>&1

timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
section() { printf '\n[%s] %s\n' "$(timestamp)" "$*"; }

capture_optional() {
  local name="$1"
  shift
  section "$name"
  "$@" >"$evidence_dir/$name.txt" 2>&1 || true
}

probe() {
  local endpoint="$1"
  local label="${endpoint#/}"
  label="${label//\//-}"
  local body_file="$evidence_dir/.${label}.body"
  local error_file="$evidence_dir/.${label}.error"
  local started_ms
  local finished_ms
  local code
  local rc=0
  started_ms="$(date +%s%3N)"
  code="$(curl --silent --show-error --connect-timeout 5 --max-time 15 \
    --output "$body_file" --write-out '%{http_code}' "$health_url$endpoint" 2>"$error_file")" || rc=$?
  finished_ms="$(date +%s%3N)"
  if [[ "$rc" -eq 0 ]]; then
    printf '%s endpoint=%s curl_rc=0 http=%s started_ms=%s finished_ms=%s\n' \
      "$(timestamp)" "$endpoint" "$code" "$started_ms" "$finished_ms"
    cat "$body_file"
    printf '\n'
  else
    printf '%s endpoint=%s curl_rc=%s http=%s started_ms=%s finished_ms=%s error=%s\n' \
      "$(timestamp)" "$endpoint" "$rc" "${code:-000}" "$started_ms" "$finished_ms" \
      "$(tr '\n' ' ' <"$error_file")"
  fi
  rm -f -- "$body_file" "$error_file"
  if [[ "$rc" -ne 0 || "$code" != 2* ]]; then
    return 1
  fi
}

collect_common() {
  capture_optional systemd-status systemctl status "$service_name" --no-pager
  capture_optional systemd-properties systemctl show "$service_name" \
    -p ActiveState -p SubState -p Result -p ExecMainStatus -p NRestarts \
    -p Restart -p RestartPreventExitStatus -p User -p ExecStart
  capture_optional disk-usage df -P /var/lib/noyra /var/log /tmp
  capture_optional mounts findmnt /var/lib/noyra
  capture_optional block-devices lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    capture_optional journal sudo -n journalctl -u "$service_name" --since "-24 hours" --no-pager
  else
    capture_optional journal journalctl -u "$service_name" --since "-24 hours" --no-pager
  fi
}

preflight() {
  section 'preflight'
  capture_optional os-release cat /etc/os-release
  capture_optional kernel uname -a
  capture_optional python-version python3 --version
  capture_optional systemd-version systemd --version
  capture_optional service-enabled systemctl is-enabled "$service_name"
  collect_common
  printf '%s evidence_dir=%s\n' "$(timestamp)" "$evidence_dir"
}

smoke() {
  section 'smoke health probes'
  systemctl is-active --quiet "$service_name"
  probe /health/live
  probe /health/ready
  probe /health
  collect_common
  printf '%s smoke=passed evidence_dir=%s\n' "$(timestamp)" "$evidence_dir"
}

soak() {
  local deadline
  local failures=0
  local samples=0
  local consecutive_failures=0
  deadline=$(( $(date +%s) + duration_seconds ))
  section "soak duration_seconds=$duration_seconds interval_seconds=$interval_seconds"
  printf 'timestamp endpoint result\n' >"$evidence_dir/health-samples.log"
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    samples=$((samples + 1))
    if probe /health/live >>"$evidence_dir/health-samples.log" 2>&1; then
      consecutive_failures=0
      printf '%s /health/live pass\n' "$(timestamp)" >>"$evidence_dir/health-samples.log"
    else
      failures=$((failures + 1))
      consecutive_failures=$((consecutive_failures + 1))
      printf '%s /health/live fail consecutive=%s\n' "$(timestamp)" "$consecutive_failures" \
        >>"$evidence_dir/health-samples.log"
    fi
    if ! probe /health/ready >>"$evidence_dir/health-samples.log" 2>&1; then
      failures=$((failures + 1))
      consecutive_failures=$((consecutive_failures + 1))
      printf '%s /health/ready fail consecutive=%s\n' "$(timestamp)" "$consecutive_failures" \
        >>"$evidence_dir/health-samples.log"
    else
      printf '%s /health/ready pass\n' "$(timestamp)" >>"$evidence_dir/health-samples.log"
    fi
    if [[ "$consecutive_failures" -ge 3 ]]; then
      printf '%s soak=aborted reason=three-consecutive-probe-failures\n' "$(timestamp)"
      collect_common
      return 1
    fi
    sleep "$interval_seconds"
  done
  collect_common
  printf '%s soak=passed samples=%s probe_failures=%s evidence_dir=%s\n' \
    "$(timestamp)" "$samples" "$failures" "$evidence_dir"
}

case "$mode" in
  preflight) preflight ;;
  smoke) smoke ;;
  soak) soak ;;
  collect) collect_common ;;
esac
