from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
NATIVE_WINDOWS = os.name == "nt"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    if script.name == "build-windows-package.ps1":
        launcher = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "whoami.exe"
        thumbprint_probe = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"(Get-AuthenticodeSignature -LiteralPath '{launcher}')."
                    "SignerCertificate.Thumbprint"
                ),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        args = (
            *args,
            "-LauncherPath",
            str(launcher),
            "-TrustedPublisherThumbprints",
            thumbprint_probe.stdout.strip(),
        )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.skipif(
    not NATIVE_WINDOWS or POWERSHELL is None,
    reason="requires native Windows Authenticode and a signed Windows launcher",
)
def test_windows_package_install_upgrade_and_rollback_are_atomic(tmp_path: Path) -> None:
    output = tmp_path / "packages"
    install = tmp_path / "install"
    data = tmp_path / "data"
    builder = ROOT / "scripts" / "build-windows-package.ps1"
    lifecycle = ROOT / "deploy" / "windows" / "noyra-lifecycle.ps1"
    recovery_helper = "deploy/windows/noyra-r03-recovery.ps1"
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
    for version in ("v1", "v2"):
        manifest = json.loads((output / version / "noyra-package.json").read_text(encoding="utf-8"))
        assert recovery_helper in {entry["path"] for entry in manifest["files"]}
        assert (output / version / recovery_helper.replace("/", os.sep)).is_file()
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


@pytest.mark.skipif(
    not NATIVE_WINDOWS or POWERSHELL is None,
    reason="requires native Windows Authenticode and a signed Windows launcher",
)
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


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("listed", [False, True])
def test_windows_package_strict_signature_cannot_bypass_launcher_inventory(
    tmp_path: Path, listed: bool
) -> None:
    assert POWERSHELL is not None
    package = tmp_path / "package"
    package.mkdir()
    launcher = package / "noyra.exe"
    launcher.write_bytes(b"synthetic non-executable launcher fixture")
    readme = package / "README.txt"
    readme.write_text("synthetic package", encoding="utf-8")
    files = [{"path": readme.name, "sha256": hashlib.sha256(readme.read_bytes()).hexdigest()}]
    if listed:
        files.append(
            {"path": launcher.name, "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest()}
        )
    (package / "noyra-package.json").write_text(
        json.dumps(
            {
                "format": "noyra-windows-package/v1",
                "version": "strict-fixture",
                "signature_required": True,
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    install, data = tmp_path / "install", tmp_path / "data"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-Action",
            "install",
            "-InstallRoot",
            str(install),
            "-DataRoot",
            str(data),
            "-PackageRoot",
            str(package),
            "-RequireSignature",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0
    assert not (install / "current.txt").exists()
    assert not (install / "versions/strict-fixture/noyra.exe").exists()


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_package_single_publisher_reaches_manifest_signature_gate(
    tmp_path: Path,
) -> None:
    assert POWERSHELL is not None
    package = tmp_path / "package"
    package.mkdir()
    launcher = package / "noyra.exe"
    launcher.write_bytes(b"synthetic non-executable launcher fixture")
    readme = package / "README.txt"
    readme.write_text("synthetic package", encoding="utf-8")
    thumbprint = "A" * 40
    (package / "noyra-package.json").write_text(
        json.dumps(
            {
                "format": "noyra-windows-package/v1",
                "version": "single-publisher-fixture",
                "signature_required": True,
                "trusted_publisher_thumbprints": [thumbprint],
                "launcher": "noyra.exe",
                "files": [
                    {
                        "path": "README.txt",
                        "sha256": hashlib.sha256(readme.read_bytes()).hexdigest(),
                    },
                    {
                        "path": "noyra.exe",
                        "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-Action",
            "install",
            "-InstallRoot",
            str(tmp_path / "install"),
            "-DataRoot",
            str(tmp_path / "data"),
            "-PackageRoot",
            str(package),
            "-RequireSignature",
            "-TrustedPublisherThumbprints",
            thumbprint,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Count" not in output
    assert "noyra-package.json.p7s" in output


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_package_missing_management_module_has_actionable_diagnostic(
    tmp_path: Path,
) -> None:
    assert POWERSHELL is not None
    script = str(ROOT / "scripts" / "build-windows-package.ps1").replace("'", "''")
    output = str(tmp_path / "packages").replace("'", "''")
    source = str(ROOT).replace(chr(39), chr(39) * 2)
    command = (
        "$env:PSModulePath = ''; "
        f"& '{script}' -Version probe -OutputRoot '{output}' -SourceRoot '{source}'; "
        "exit $LASTEXITCODE"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 78
    assert "NOYRA-WINDOWS-PACKAGE-PREREQUISITE[PS-MANAGEMENT-MODULE]:" in (
        result.stdout + result.stderr
    )
    assert not (tmp_path / "packages").exists()
