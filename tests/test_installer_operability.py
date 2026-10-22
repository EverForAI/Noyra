from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/install-ubuntu.sh").read_text(encoding="utf-8")
LINUX_ROOT = os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0


def _reporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "installer_report", ROOT / "scripts/installer-report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_helpers_use_only_the_new_isolated_environment() -> None:
    assert 'python3 "$SOURCE_DIR/scripts/deployment-maintenance.py"' not in SOURCE
    staged = '"$staging/.venv/bin/python" -I '
    installed = '"$release_root/.venv/bin/python" -I '
    assert staged + '"$SOURCE_DIR/scripts/deployment-maintenance.py" preflight' in SOURCE
    assert installed + '"$release_root/scripts/deployment-maintenance.py" mark-release' in SOURCE
    assert installed + '"$release_root/scripts/deployment-maintenance.py" gc' in SOURCE
    assert SOURCE.index("install_phase=storage_preflight") < SOURCE.index("\nstop_old_service\n")
    assert SOURCE.index("install_phase=service_user_import_staged") < SOURCE.index(
        "\nstop_old_service\n"
    )
    assert 'verify_service_user_import "$staging/.venv/bin/python"' in SOURCE
    assert 'verify_service_user_import "$release_root/.venv/bin/python"' in SOURCE
    assert SOURCE.index("install_phase=service_user_import") < SOURCE.index(
        "\nmigrate_database_before_switch\n"
    )
    assert (
        "'0644:deployment-maintenance.py'" in (ROOT / "scripts/lib/release-scripts.sh").read_text()
    )


def test_installer_preserves_stop_intent_and_distinguishes_unverified_success() -> None:
    assert "--start)" in SOURCE
    assert '"$service_active" == true || "$start_requested" == true' in SOURCE
    assert "Readiness has NOT been verified." in SOURCE
    assert 'if [[ "$readiness_verified" == true ]]; then\n  maintenance_backup=()' in SOURCE
    assert 'on_error "$status" from-exit || status=$?' in SOURCE
    assert 'write_install_report "$status"' in SOURCE
    assert SOURCE.index("install_phase=rollback_compatibility") < SOURCE.index(
        "install_phase=rollback_switch"
    )
    assert "database_recovery_required=true" in SOURCE


def test_report_rejects_non_absolute_and_traversal_destinations(tmp_path: Path) -> None:
    helper = _reporter()
    for directory, name in ((Path("relative"), "report"), (tmp_path, "../escape")):
        with pytest.raises(ValueError, match="invalid_report_location"):
            helper.write_report(directory, name, {})


@pytest.mark.skipif(
    os.name == "posix", reason="Linux root writer is exercised with real ownership below"
)
def test_atomic_report_is_bounded_and_never_overwrites(tmp_path: Path) -> None:
    helper = _reporter()
    directory = tmp_path / "reports"
    payload: dict[str, Any] = {"format": "noyra-install-report/v1", "exit_code": 1}
    path = helper.write_report(directory, "attempt", payload)
    assert json.loads(path.read_text()) == payload
    with pytest.raises(ValueError, match="already_exists"):
        helper.write_report(directory, "attempt", {})
    with pytest.raises(ValueError, match="too_large"):
        helper.write_report(directory, "large", {"data": "x" * 20_000})
    assert not (directory / "large.json").exists()
    assert not list(directory.glob(".install-report-*"))


@pytest.mark.skipif(not LINUX_ROOT, reason="requires Linux root and a real service user")
def test_public_build_ignores_restrictive_umask_without_widening_private_files() -> None:
    with tempfile.TemporaryDirectory(prefix="noyra-install-umask-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        source = root / "source"
        source.mkdir()
        (source / "requirements.lock").write_text("")
        secret = root / "private"
        secret.write_text("not-a-real-secret")
        secret.chmod(0o600)
        start = SOURCE.index("(\n  trap - EXIT ERR\n  umask 0022")
        end = SOURCE.index("\ninstall_phase=service_user_import_staged", start)
        build = SOURCE[start:end]
        helper_start = SOURCE.index("verify_service_user_import() {")
        helper_end = SOURCE.index("\nverify_release_schema_contract() {", helper_start)
        helper = SOURCE[helper_start:helper_end]
        script = (
            r"""set -euo pipefail
umask 0077
staging="$1/release"
SOURCE_DIR="$1/source"
profile=base
python3() {
  command python3 -m venv --without-pip "$3"
  local site
  site=$("$3/bin/python" -I -c 'import sysconfig;print(sysconfig.get_path("purelib"))')
  mkdir -p "$site/noyra/core"
  printf '__version__="test"\n' > "$site/noyra/__init__.py"
  printf 'class ServiceSettings: pass\n' > "$site/noyra/service.py"
  printf 'class Database: pass\n' > "$site/noyra/core/database.py"
  printf '# isolated no-op pip for this offline permission fixture\n' > "$site/pip.py"
}
noyra_install_release_scripts() { test -r "$2/.venv/pyvenv.cfg"; }
"""
            + build
            + "\n"
            + helper
            + r"""
test "$(umask)" = 0077
runuser() {
  test "$1" = --user=noyra && test "$2" = --group=noyra && test "$3" = --
  shift 3
  command runuser -u nobody -- "$@"
}
verify_service_user_import "$staging/.venv/bin/python"
"""
        )
        result = subprocess.run(
            ["bash", "-c", script, "_", str(root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert stat.S_IMODE((root / "release/.venv/pyvenv.cfg").stat().st_mode) == 0o644
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600


@pytest.mark.skipif(not LINUX_ROOT, reason="requires Linux root-owned report directory")
def test_report_cli_preserves_failure_and_never_claims_unrun_readiness() -> None:
    # /tmp is deliberately not accepted as a trusted root-owned install ancestry.
    with tempfile.TemporaryDirectory(prefix="noyra-report-", dir="/root") as temporary:
        root = Path(temporary)
        directory = root / "reports"
        args = [
            "python3",
            "-I",
            str(ROOT / "scripts/installer-report.py"),
            "--directory",
            str(directory),
            "--report-id",
            "failed-attempt",
            "--started-at",
            "2026-10-22T00:00:00Z",
            "--exit-code",
            "7",
            "--phase",
            "migrate_database",
            "--service-state",
            "stopped",
        ]
        for flag in (
            "backup-verified",
            "migration-started",
            "migration-completed",
            "readiness-verified",
            "service-observed",
            "previously-running",
            "start-requested",
            "rollback",
            "database-recovery-required",
        ):
            args.extend(["--" + flag, "true" if flag == "migration-started" else "false"])
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        report = directory / "failed-attempt.json"
        data = json.loads(report.read_text())
        assert data["exit_code"] == 7 and data["outcome"] == "failed"
        assert data["migration_started"] and not data["readiness_verified"]
        assert not data["external_eight_gates_performed"]
        assert stat.S_IMODE(report.stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        # Symlinked report directories cannot redirect a privileged write.
        link = root / "linked"
        link.symlink_to(directory, target_is_directory=True)
        with pytest.raises(ValueError, match="unsafe_report_directory"):
            _reporter().write_report(link, "injected", {})


@pytest.mark.skipif(not LINUX_ROOT, reason="requires Bash and a real SQLite path")
def test_failed_post_migration_publication_keeps_compatible_runtime_and_stops_old_code() -> None:
    start = SOURCE.index("on_error() {")
    end = SOURCE.index("\ntrap on_error ERR", start)
    function = SOURCE[start:end]
    with tempfile.TemporaryDirectory(prefix="noyra-recovery-") as temporary:
        root = Path(temporary)
        data = root / "data"
        release = root / "new"
        data.mkdir()
        release.mkdir()
        (data / "noyra.sqlite3").touch()
        (release / "keep").write_text("runtime")
        script = (
            r"""set -euo pipefail
CONTROL_LAYOUT_CHANGED=false
UPGRADE_COMPONENTS_CHANGED=false
MIGRATION_COMPONENTS_CHANGED=false
cleanup_failed=false
upgrade_components_restored=true
migration_components_restored=true
switched=false
release_published=true
migration_started=true
service_was_stopped=true
legacy_moved=false
old_current=old
DATA_DIR="$1/data"
release_root="$1/new"
RELEASES_DIR="$1"
cleanup_staging(){ :; }
noyra_control_layout_restore(){ :; }
restore_profile_dropin_after_failure(){ :; }
verify_release_schema_contract(){ return 1; }
systemctl(){ echo "UNEXPECTED_SERVICE_RESTART"; return 1; }
"""
            + function
            + "\non_error 9\n"
        )
        result = subprocess.run(
            ["bash", "-c", script, "_", str(root)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 9
        assert (release / "keep").is_file()
        assert "UNEXPECTED_SERVICE_RESTART" not in result.stdout
        assert "leaving Noyra stopped" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="requires Bash")
@pytest.mark.parametrize(
    ("systemd_state", "expected"),
    [
        ("active", "running"),
        ("inactive", "stopped"),
        ("failed", "stopped"),
        ("activating", "unknown"),
        ("", "unknown"),
    ],
)
def test_report_service_state_never_invents_a_stopped_service(
    systemd_state: str,
    expected: str,
) -> None:
    start = SOURCE.index("write_install_report() {")
    end = SOURCE.index("\n\nservice_active=false", start)
    script = (
        r"""set -euo pipefail
fake_state="$1"
SOURCE_DIR=/synthetic/source
INSTALL_DIR=/synthetic/install
install_report_id=test
install_started_at=2026-10-22T00:00:00Z
install_phase=prepare
old_current=""
backup_path=""
backup_restore_verified=false
migration_started=false
migration_completed=false
readiness_verified=false
service_observed=false
service_active=false
start_requested=false
rollback=false
database_recovery_required=false
systemctl() { printf '%s' "$fake_state"; return 3; }
python3() { printf '%s\n' "$@"; }
"""
        + SOURCE[start:end]
        + "\nwrite_install_report 7\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "_", systemd_state],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[args.index("--service-state") + 1] == expected
    assert args[args.index("--exit-code") + 1] == "7"


@pytest.mark.skipif(os.name != "posix", reason="requires Bash")
@pytest.mark.parametrize(
    ("original_status", "cleanup_fail", "report_fail", "expected"),
    [
        (0, False, False, 0),
        (7, False, False, 7),
        (0, False, True, 1),
        (7, False, True, 7),
        (0, True, False, 1),
    ],
)
def test_exit_trap_preserves_failure_and_requires_a_report(
    original_status: int,
    cleanup_fail: bool,
    report_fail: bool,
    expected: int,
) -> None:
    start = SOURCE.index("on_exit() {")
    end = SOURCE.index("\ntrap on_exit EXIT", start)
    script = (
        r"""set -euo pipefail
cleanup_requested="$2"
report_requested="$3"
cleanup_failed=false
failure_handled=false
on_error() { printf 'RECOVERY:%s:%s\n' "$1" "$2"; failure_handled=true; return "$1"; }
cleanup_staging() { cleanup_failed="$cleanup_requested"; }
write_install_report() { printf 'REPORT:%s\n' "$1"; test "$report_requested" = false; }
"""
        + SOURCE[start:end]
        + '\ntrap on_exit EXIT\nexit "$1"\n'
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "_",
            str(original_status),
            str(cleanup_fail).lower(),
            str(report_fail).lower(),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    assert f"REPORT:{1 if cleanup_fail else original_status}" in result.stdout
    assert ("RECOVERY:" in result.stdout) == (original_status != 0)
    assert ("Could not persist installation report" in result.stderr) == report_fail


def test_ci_runs_installer_operability_under_real_linux_root() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    root_step = workflow.split("- name: Verify Ubuntu installer backup boundary", 1)[1]
    lines = root_step.splitlines()
    index = next(i for i, line in enumerate(lines) if "tests/test_installer_operability.py" in line)
    assert lines[index - 1].strip() == 'sudo "$(command -v python)" -m pytest ' + chr(92)
