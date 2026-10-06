from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sysconfig
import tempfile
import venv
from collections.abc import Iterator
from pathlib import Path

import pytest

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
from noyra.migration import activation

release = Path(sys.argv[1])
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
    data_root.mkdir()
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
