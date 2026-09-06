from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_package_install_upgrade_and_rollback_are_atomic(tmp_path: Path) -> None:
    output = tmp_path / "packages"
    install = tmp_path / "install"
    data = tmp_path / "data"
    builder = ROOT / "scripts" / "build-windows-package.ps1"
    lifecycle = ROOT / "deploy" / "windows" / "noyra-lifecycle.ps1"
    for version in ("v1", "v2"):
        _run(
            builder,
            "-Version",
            version,
            "-OutputRoot",
            str(output),
            "-SourceRoot",
            str(ROOT),
        )
    _run(
        lifecycle,
        "-Action",
        "install",
        "-InstallRoot",
        str(install),
        "-DataRoot",
        str(data),
        "-PackageRoot",
        str(output / "v1"),
    )
    _run(
        lifecycle,
        "-Action",
        "upgrade",
        "-InstallRoot",
        str(install),
        "-DataRoot",
        str(data),
        "-PackageRoot",
        str(output / "v2"),
    )
    assert (install / "current.txt").read_text(encoding="utf-8").strip() == "v2"
    status = _run(
        lifecycle,
        "-Action",
        "status",
        "-InstallRoot",
        str(install),
        "-DataRoot",
        str(data),
    )
    assert json.loads(status.stdout)["previous_version"] == "v1"
    _run(
        lifecycle,
        "-Action",
        "rollback",
        "-InstallRoot",
        str(install),
        "-DataRoot",
        str(data),
    )
    assert (install / "current.txt").read_text(encoding="utf-8").strip() == "v1"


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_package_hash_tampering_is_rejected(tmp_path: Path) -> None:
    assert POWERSHELL is not None
    powershell = POWERSHELL
    output = tmp_path / "packages"
    install = tmp_path / "install"
    data = tmp_path / "data"
    builder = ROOT / "scripts" / "build-windows-package.ps1"
    lifecycle = ROOT / "deploy" / "windows" / "noyra-lifecycle.ps1"
    _run(
        builder,
        "-Version",
        "tamper",
        "-OutputRoot",
        str(output),
        "-SourceRoot",
        str(ROOT),
    )
    manifest = output / "tamper" / "noyra-package.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    first = document["files"][0]["path"]
    (output / "tamper" / first).write_bytes(b"tampered")
    assert not (install / "current.txt").exists()
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(lifecycle),
            "-Action",
            "install",
            "-InstallRoot",
            str(install),
            "-DataRoot",
            str(data),
            "-PackageRoot",
            str(output / "tamper"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0
    assert not (install / "current.txt").exists()
