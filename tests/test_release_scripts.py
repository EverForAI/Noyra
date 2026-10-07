from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sysconfig
import tempfile
import venv
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from noyra.migration import activation

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires real Linux root and service-account permissions",
)


@pytest.fixture
def release_tree() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="noyra-release-scripts-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        source = root / "source"
        source.mkdir()
        shutil.copytree(ROOT / "scripts", source / "scripts")
        release = root / "release"
        venv.EnvBuilder(system_site_packages=True, with_pip=False).create(release / ".venv")
        python = release / ".venv/bin/python"
        site = Path(
            subprocess.check_output(
                [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True,
                timeout=30,
            ).strip()
        )
        shutil.copytree(
            ROOT / "src/noyra",
            site / "noyra",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for path in (site / "noyra").rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        # Dependencies come from the test environment; application imports
        # resolve to the independent packaged copy, including for nobody.
        dependency_sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
        (site / "test-dependencies.pth").write_text(
            "\n".join(dependency_sites) + "\n", encoding="utf-8"
        )
        yield source, release


def _install(source: Path, release: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; noyra_install_release_scripts "$2" "$3"',
            "_",
            str(ROOT / "scripts/lib/release-scripts.sh"),
            str(source),
            str(release),
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_packaged_entrypoints_import_and_service_user_can_run_agent(
    release_tree: tuple[Path, Path],
) -> None:
    source, release = release_tree
    result = _install(source, release)
    assert result.returncode == 0, result.stderr
    expected = {
        "preflight-production.py": 0o644,
        "noyra-target-activation-runner.py": 0o750,
        "noyra-migration-agent.py": 0o755,
    }
    for name, mode in expected.items():
        path = release / "scripts" / name
        metadata = path.stat()
        assert stat.S_IMODE(metadata.st_mode) == mode
        assert metadata.st_uid == metadata.st_gid == 0
        assert path.read_bytes() == (source / "scripts" / name).read_bytes()

    for name in ("noyra-migration-agent.py", "preflight-production.py"):
        help_result = subprocess.run(
            [
                "runuser",
                "-u",
                "nobody",
                "--",
                str(release / ".venv/bin/python"),
                "-I",
                str(release / "scripts" / name),
                "--help",
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert help_result.returncode == 0, help_result.stderr
        assert "usage:" in help_result.stdout

    recovery_code = """
import functools
import runpy
import sys
from pathlib import Path
import pwd
from noyra.migration import activation

release = Path(sys.argv[1])
service = pwd.getpwnam("nobody")
activation._service_uid = lambda: service.pw_uid
activation._service_gid = lambda: service.pw_gid
activation.recover_incomplete_target_activations = functools.partial(
    activation.recover_incomplete_target_activations,
    data_root=release.parent / "data",
    config_root=release.parent / "etc/noyra",
)
runner = release / "scripts/noyra-target-activation-runner.py"
sys.argv = [str(runner), "--recover"]
runpy.run_path(str(runner), run_name="__main__")
"""
    data_root = release.parent / "data"
    data_root.mkdir(mode=0o700)
    accounts: Any = import_module("pwd")
    service = accounts.getpwnam("nobody")
    chown = getattr(os, "chown", None)
    assert callable(chown)
    chown(data_root, service.pw_uid, service.pw_gid)
    recovery_path = release.parent / "recover.py"
    recovery_path.write_text(recovery_code, encoding="utf-8")
    recovery = subprocess.run(
        [
            "bash",
            "-c",
            """
set -euo pipefail
source "$1"
data="$2"
state="$data/migration/target-activation/state"
noyra_control_layout_snapshot "$data" "$data/migration" "$data/migration/target-activation" \
  "$state" "$state/rollback" "$state/activations" "$state/control.lock"
install -d -o root -g "$(stat -c '%g' "$data")" -m 0750 \
  "$data/migration" "$data/migration/target-activation"
noyra_control_layout_prepare_activation_state "$data"
"$3/.venv/bin/python" -I "$4" "$3"
noyra_control_layout_restore
[[ ! -e "$data/migration" ]]
""",
            "_",
            str(ROOT / "scripts/lib/control-layout.sh"),
            str(data_root),
            str(release),
            str(recovery_path),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert recovery.returncode == 0, recovery.stderr
    assert data_root.stat().st_uid == service.pw_uid
    assert stat.S_IMODE(data_root.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "tamper",
    [
        "data_owner",
        "data_group",
        "data_readable",
        "data_writable",
        "control_owner",
        "control_writable",
        "state_owner",
        "control_symlink",
    ],
)
def test_recovery_rejects_unsafe_installed_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    accounts: Any = import_module("pwd")
    service = accounts.getpwnam("nobody")
    chown = getattr(os, "chown", None)
    assert callable(chown)
    monkeypatch.setattr(activation, "_service_uid", lambda: service.pw_uid)
    monkeypatch.setattr(activation, "_service_gid", lambda: service.pw_gid)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    chown(data, service.pw_uid, service.pw_gid)
    control = data / "migration"
    bridge = control / "target-activation"
    for directory in (control, bridge):
        directory.mkdir(mode=0o750)
        chown(directory, 0, service.pw_gid)
    state = bridge / "state"
    state.mkdir(mode=0o700)
    if tamper == "data_owner":
        chown(data, service.pw_uid + 1, service.pw_gid)
    elif tamper == "data_group":
        chown(data, service.pw_uid, 0)
    elif tamper == "data_readable":
        data.chmod(0o750)
    elif tamper == "data_writable":
        data.chmod(0o770)
    elif tamper == "control_owner":
        chown(control, service.pw_uid, service.pw_gid)
    elif tamper == "control_writable":
        control.chmod(0o770)
    elif tamper == "state_owner":
        chown(state, service.pw_uid, service.pw_gid)
    else:
        control.rename(data / "original-migration")
        control.symlink_to(data / "original-migration", target_is_directory=True)

    activator = activation.TargetRuntimeActivator(data, tmp_path / "config")
    with pytest.raises(
        activation.TargetActivationError, match="activation_state_directory_invalid"
    ):
        activator.recover_incomplete()
    assert not (state / "control.lock").exists()
    assert not (state / "rollback").exists()


def test_service_owned_boundary_is_explicit_and_cannot_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts: Any = import_module("pwd")
    service = accounts.getpwnam("nobody")
    chown = getattr(os, "chown", None)
    assert callable(chown)
    monkeypatch.setattr(activation, "_service_uid", lambda: service.pw_uid)
    monkeypatch.setattr(activation, "_service_gid", lambda: service.pw_gid)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    chown(data, service.pw_uid, service.pw_gid)
    state = data / "state"
    state.mkdir(mode=0o700)

    with pytest.raises(activation.TargetActivationError, match="invalid"):
        activation._secure_root_directory(state, "invalid", trusted_root=data)
    for path in (tmp_path / "outside", data / ".." / "outside"):
        with pytest.raises(activation.TargetActivationError, match="invalid"):
            activation._secure_root_directory(
                path, "invalid", create=True, trusted_root=data, allow_service_owned_root=True
            )
    assert not (tmp_path / "outside").exists()
    link = tmp_path / "data-link"
    link.symlink_to(data, target_is_directory=True)
    with pytest.raises(activation.TargetActivationError, match="invalid"):
        activation._secure_root_directory(
            link / "state", "invalid", trusted_root=link, allow_service_owned_root=True
        )


def test_idle_recovery_scaffolding_does_not_discard_runtime_records(
    release_tree: tuple[Path, Path],
) -> None:
    _, release = release_tree
    data = release.parent / "data"
    data.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
set -euo pipefail
source "$1"
data="$2"
state="$data/migration/target-activation/state"
noyra_control_layout_snapshot "$data" "$data/migration" "$data/migration/target-activation" \
  "$state" "$state/rollback" "$state/activations" "$state/control.lock"
noyra_control_layout_prepare_activation_state "$data"
printf 'runtime-activation' > "$state/activations/runtime.json"
if noyra_control_layout_restore; then
  exit 1
fi
[[ "$(<"$state/activations/runtime.json")" == runtime-activation ]]
""",
            "_",
            str(ROOT / "scripts/lib/control-layout.sh"),
            str(data),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("invalid_source", ["missing", "symlink", "bad_import"])
def test_packaging_rejects_missing_unsafe_or_unimportable_entrypoint(
    release_tree: tuple[Path, Path], invalid_source: str
) -> None:
    source, release = release_tree
    runner = source / "scripts/noyra-target-activation-runner.py"
    if invalid_source == "bad_import":
        runner.write_text("import missing_noyra_release_dependency\n", encoding="utf-8")
    else:
        runner.unlink()
        if invalid_source == "symlink":
            runner.symlink_to(source / "scripts/noyra-migration-agent.py")
    result = _install(source, release)
    assert result.returncode != 0
    expected = (
        "missing_noyra_release_dependency" if invalid_source == "bad_import" else "regular file"
    )
    assert expected in result.stderr
