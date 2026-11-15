from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Ubuntu Bash cleanup protocol")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/install-ubuntu.sh").read_text(encoding="utf-8")
ON_ERROR = SOURCE[SOURCE.index("on_error() {") : SOURCE.index("\ntrap on_error ERR")]
ON_EXIT = SOURCE[SOURCE.index("on_exit() {") : SOURCE.index("\ntrap on_exit EXIT")]

SETUP = r"""set -euo pipefail
root="$1"
trigger="$2"
mount_mode="$3"
remove_mode="$4"
migration_started="$5"
schema_compatible="$6"
switched="$7"
CONTROL_LAYOUT_CHANGED=false
UPGRADE_COMPONENTS_CHANGED=false
MIGRATION_COMPONENTS_CHANGED=false
cleanup_failed=false
failure_handled=false
upgrade_components_restored=true
migration_components_restored=true
release_published=true
service_was_stopped=true
service_active=true
legacy_moved=false
database_recovery_required=false
old_current=old
backup_path=""
DATA_DIR="$root/data"
RELEASES_DIR="$root/releases"
release_root="$RELEASES_DIR/failed-release"
cleanup_staging(){ :; }
noyra_control_layout_restore(){ :; }
restore_profile_dropin_after_failure(){ :; }
restore_pointers_after_failure(){ :; }
verify_release_schema_contract(){ [[ "$schema_compatible" == true ]]; }
systemctl(){ printf '%s\n' "$*" >> "$root/service.log"; }
write_install_report(){
  printf '%s:%s:%s:%s\n' "$1" "$cleanup_failed" "$service_was_stopped" \
    "$database_recovery_required" > "$root/report"
}
findmnt(){
  printf '%s\0' "$@" >> "$root/findmnt.args"
  case "$mount_mode" in
    real) command findmnt "$@" ;;
    unavailable) return 4 ;;
    empty) return 0 ;;
    *) cat "$root/mounts" ;;
  esac
}
rm(){
  # Every actual deletion is confined to this disposable release operand.
  [[ "${!#}" == "$root/releases/failed-release" ]] || return 99
  printf '%s\0' "$@" >> "$root/rm.args"
  [[ "$remove_mode" != fail ]] || return 73
  command rm "$@"
}
"""
SCRIPT = (
    SETUP
    + ON_ERROR
    + "\n"
    + ON_EXIT
    + "\ntrap on_error ERR\ntrap on_exit EXIT\n"
    + 'if [[ "$trigger" == explicit-exit ]]; then exit 9; fi\n(exit 9)\n'
)


def workspace(tmp_path: Path, *, spaced: bool = False) -> Path:
    root = tmp_path / ("workspace with space" if spaced else "workspace")
    release = root / "releases/failed-release"
    release.mkdir(parents=True)
    (release / "runtime").write_bytes(b"disposable-new-code")
    (root / "releases/old").mkdir()
    (root / "releases/old/runtime").write_bytes(b"old-code")
    (root / "data").mkdir()
    (root / "data/noyra.sqlite3").write_bytes(b"synthetic-data-do-not-delete")
    (root / "encrypted-backup").write_bytes(b"synthetic-backup-do-not-delete")
    (root / "mounts").write_text("/\n", encoding="utf-8")
    return root


def invoke(
    root: Path,
    *,
    trigger: str = "error",
    mount_mode: str = "listed",
    remove_mode: str = "normal",
    migration: bool = True,
    compatible: bool = True,
    switched: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            SCRIPT,
            "installer-cleanup-test",
            str(root),
            trigger,
            mount_mode,
            remove_mode,
            str(migration).lower(),
            str(compatible).lower(),
            str(switched).lower(),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def assert_subject_retained(root: Path) -> None:
    assert (root / "data/noyra.sqlite3").read_bytes() == b"synthetic-data-do-not-delete"
    assert (root / "encrypted-backup").read_bytes() == b"synthetic-backup-do-not-delete"
    assert (root / "releases/old/runtime").read_bytes() == b"old-code"


def raw_mount(path: Path) -> str:
    return str(path).replace("\\", r"\x5c").replace(" ", r"\x20").replace("\n", r"\x0a")


@pytest.mark.parametrize("trigger", ["error", "explicit-exit"])
@pytest.mark.parametrize("migration", [False, True])
def test_unmounted_failed_release_is_removed_with_filesystem_boundary_and_old_service_recovers(
    tmp_path: Path, trigger: str, migration: bool
) -> None:
    root = workspace(tmp_path, spaced=True)
    result = invoke(root, trigger=trigger, migration=migration)
    assert result.returncode == 9, result.stderr
    assert not (root / "releases/failed-release").exists()
    assert (root / "rm.args").read_bytes().split(b"\0")[:-1] == [
        b"-rf",
        b"--one-file-system",
        b"--",
        os.fsencode(root / "releases/failed-release"),
    ]
    assert (root / "service.log").read_text().splitlines() == ["start noyra"]
    assert (root / "report").read_text().strip() == "9:false:true:false"
    assert_subject_retained(root)


@pytest.mark.parametrize("trigger", ["error", "explicit-exit"])
@pytest.mark.parametrize("point", ["root", "child"])
def test_mount_at_or_below_failed_release_prevents_any_deletion_or_restart(
    tmp_path: Path, trigger: str, point: str
) -> None:
    root = workspace(tmp_path, spaced=True)
    release = root / "releases/failed-release"
    mounted = release if point == "root" else release / "nested mount"
    (root / "mounts").write_text("/\n" + raw_mount(mounted) + "\n", encoding="utf-8")
    result = invoke(root, trigger=trigger)
    assert result.returncode == 1, result.stderr
    assert (release / "runtime").read_bytes() == b"disposable-new-code"
    assert not (root / "rm.args").exists()
    assert not (root / "service.log").exists()
    assert (root / "report").read_text().strip() == "1:true:false:false"
    assert "mount" in result.stderr.lower() and "stopped" in result.stderr.lower()
    assert_subject_retained(root)


@pytest.mark.parametrize("trigger", ["error", "explicit-exit"])
@pytest.mark.parametrize("failure", ["unavailable", "empty", "remove"])
def test_cleanup_failure_reaches_exit_report_and_leaves_service_stopped(
    tmp_path: Path, trigger: str, failure: str
) -> None:
    root = workspace(tmp_path)
    result = invoke(
        root,
        trigger=trigger,
        mount_mode=failure if failure != "remove" else "listed",
        remove_mode="fail" if failure == "remove" else "normal",
    )
    assert result.returncode == 1, result.stderr
    assert (root / "releases/failed-release/runtime").is_file()
    assert not (root / "service.log").exists()
    assert (root / "report").read_text().strip() == "1:true:false:false"
    assert (root / "rm.args").exists() == (failure == "remove")
    assert_subject_retained(root)


def test_sibling_mount_and_symlink_do_not_block_or_expand_cleanup(tmp_path: Path) -> None:
    root = workspace(tmp_path, spaced=True)
    sibling = root / "releases/failed-release-sibling"
    sibling.mkdir()
    (sibling / "keep").write_bytes(b"sibling-data")
    (root / "releases/failed-release/link").symlink_to(sibling, target_is_directory=True)
    (root / "mounts").write_text("/\n" + raw_mount(sibling) + "\n", encoding="utf-8")
    result = invoke(root)
    assert result.returncode == 9, result.stderr
    assert not (root / "releases/failed-release").exists()
    assert (sibling / "keep").read_bytes() == b"sibling-data"
    assert_subject_retained(root)


@pytest.mark.parametrize("switched", [False, True])
def test_new_cleanup_keeps_schema_and_pointer_recovery_gates(
    tmp_path: Path, switched: bool
) -> None:
    root = workspace(tmp_path)
    result = invoke(root, compatible=switched, switched=switched)
    assert result.returncode == 9, result.stderr
    assert (root / "releases/failed-release/runtime").is_file()
    assert not (root / "findmnt.args").exists()
    assert not (root / "rm.args").exists()
    report = (root / "report").read_text().strip()
    if switched:
        assert report == "9:false:true:false"
        assert "start noyra" in (root / "service.log").read_text()
    else:
        assert report == "9:false:false:true"
        assert not (root / "service.log").exists()
    assert_subject_retained(root)


@pytest.fixture(scope="module")
def mount_namespace() -> str:
    if getattr(os, "geteuid", lambda: -1)() != 0:
        pytest.skip("requires root in a private Linux mount namespace")
    probe = subprocess.run(
        ["unshare", "--mount", "--propagation", "private", "/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode:
        pytest.skip("private mount namespaces are unavailable: " + probe.stderr.strip())
    return os.readlink("/proc/self/ns/mnt")


@pytest.mark.parametrize("kind", ["different-device", "same-device-bind", "root-bind"])
@pytest.mark.parametrize("trigger", ["error", "explicit-exit"])
def test_real_private_mounts_keep_payload_and_failed_release(
    tmp_path: Path, mount_namespace: str, kind: str, trigger: str
) -> None:
    root = workspace(tmp_path, spaced=True)
    outer = r"""set -euo pipefail
handler="$1"; root="$2"; kind="$3"; trigger="$4"; parent_namespace="$5"
[[ "$(readlink /proc/self/ns/mnt)" != "$parent_namespace" ]] || exit 90
release="$root/releases/failed-release"
foreign="$root/foreign"
point="$release/nested"
mkdir -p "$foreign" "$point"
if [[ "$kind" == different-device ]]; then
  mount -t tmpfs -o size=1m noyra-m14-test "$foreign"
fi
printf 'protected-mount-payload' > "$foreign/keep"
if [[ "$kind" == root-bind ]]; then point="$release"; fi
mount --bind "$foreign" "$point"
if [[ "$kind" == different-device ]]; then
  [[ "$(stat -c %d "$point")" != "$(stat -c %d "$root")" ]] || exit 91
else
  [[ "$(stat -c %d "$point")" == "$(stat -c %d "$root")" ]] || exit 92
fi
status=0
bash -c "$handler" installer-cleanup-native "$root" "$trigger" real normal true true false \
  || status=$?
if [[ -f "$foreign/keep" ]]; then payload=present; else payload=missing; fi
printf 'native-handler-status=%s; payload=%s\n' "$status" "$payload"
[[ "$status" == 1 ]] || exit 93
[[ "$(cat "$foreign/keep")" == protected-mount-payload ]] || exit 94
[[ ! -e "$root/rm.args" && ! -e "$root/service.log" ]] || exit 95
printf 'native-private-mount-preserved\n'
"""
    result = subprocess.run(
        [
            "unshare",
            "--mount",
            "--propagation",
            "private",
            "bash",
            "-c",
            outer,
            "installer-cleanup-namespace",
            SCRIPT,
            str(root),
            kind,
            trigger,
            mount_namespace,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "native-private-mount-preserved" in result.stdout
    assert os.readlink("/proc/self/ns/mnt") == mount_namespace
    # The namespace is gone before pytest inspects or cleans the fixture paths.
    assert (root / "releases/failed-release/runtime").read_bytes() == b"disposable-new-code"
    assert (root / "report").read_text().strip() == "1:true:false:false"
    assert_subject_retained(root)
