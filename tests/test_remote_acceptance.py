from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else "D:/Git/bin/bash.exe"
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="requires Bash")


@pytest.mark.parametrize(
    ("live", "ready", "final", "expected", "status"),
    [
        ("200 200 200 200", "200 200 200 200", "200", 0, "passed"),
        ("200 200 200 200", "503 503 503 503", "200", 1, "aborted"),
        ("503 503 503 503", "200 200 200 200", "200", 1, "aborted"),
        ("503 200 503 200", "200 503 200 503", "200", 1, "aborted"),
        ("200 200 200 200", "503 200 200 200", "200", 1, "degraded"),
        ("200 200 200 200", "200 200 200 503", "200", 1, "degraded"),
        ("200 200 200 200", "timeout timeout timeout timeout", "200", 1, "aborted"),
        ("200 200 200 200", "302 302 302 302", "200", 1, "aborted"),
        ("200 200 200 200", "200 200 200 200", "503", 1, "failed"),
    ],
)
def test_soak_preserves_any_failed_round(
    tmp_path: Path, live: str, ready: str, final: str, expected: int, status: str
) -> None:
    # Run the complete production script with local shell functions replacing
    # time, HTTP and host collection; no real systemd or network actions occur.
    harness = r"""
date() {
  if [[ "$1" == +%s ]]; then
    local current=0
    [[ ! -f "$CLOCK" ]] || read -r current <"$CLOCK"
    printf '%s\n' "$((current + 1))" >"$CLOCK"
    printf '%s\n' "$current"
  elif [[ "$1" == +%s%3N ]]; then
    printf '1000\n'
  else
    command date "$@"
  fi
}
sleep() { :; }
curl() {
  local output url tick index code
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output) output="$2"; shift 2 ;;
      --connect-timeout|--max-time|--write-out) shift 2 ;;
      --*) shift ;;
      *) url="$1"; shift ;;
    esac
  done
  read -r tick <"$CLOCK"
  index=$((tick - 2))
  local -a codes
  if [[ "$url" == */health/live ]]; then
    read -ra codes <<<"$LIVE"
  elif [[ "$url" == */health/ready ]]; then
    read -ra codes <<<"$READY"
  else
    codes=("$FINAL")
    index=0
  fi
  code="${codes[$index]:-200}"
  printf '{}\n' >"$output"
  if [[ "$code" == timeout ]]; then
    printf '000'
    return 28
  fi
  printf '%s' "$code"
}
systemctl() { :; }
df() { :; }
findmnt() { :; }
lsblk() { :; }
journalctl() { :; }
sudo() { return 1; }
source "$1" soak 5 "$2"
"""
    environment = os.environ.copy()
    environment.update(CLOCK=(tmp_path / "clock").as_posix(), LIVE=live, READY=ready, FINAL=final)
    result = subprocess.run(
        [
            str(BASH),
            "-c",
            harness,
            "test",
            (ROOT / "scripts/remote-acceptance.sh").as_posix(),
            (tmp_path / "evidence").as_posix(),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert f"soak={status}" in result.stdout
    if status != "passed":
        assert "soak=passed" not in result.stdout
        if status != "failed":
            assert " fail" in (tmp_path / "evidence/health-samples.log").read_text()
