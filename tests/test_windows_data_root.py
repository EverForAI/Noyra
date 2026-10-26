from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noyra.core.managed_data_root import managed_data_root
from noyra.core.types import content_hash
from noyra.service import NoyraService, ServiceSettings

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh")


def _environment(root: Path, *, initialize: bool = False) -> dict[str, str]:
    data = root / "retained data 数据"
    return {
        "NOYRA_MANAGED_INSTALL_ROOT": str(root / "installed versions"),
        "NOYRA_MANAGED_DATA_ROOT": str(data),
        "NOYRA_DATA_DIR": str(data),
        "NOYRA_MANAGED_INITIALIZE": "1" if initialize else "0",
    }


def _database(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / "noyra.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
        connection.execute("INSERT INTO fixture VALUES('existing subject data')")
    return target


def _install_environment(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> None:
    for name in os.environ:
        if name.startswith("NOYRA_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def test_unmanaged_configuration_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert managed_data_root(environ={"NOYRA_DATA_DIR": "relative-development-data"}) is None
    _install_environment(monkeypatch, {"NOYRA_DATA_DIR": "relative-development-data"})
    monkeypatch.chdir(tmp_path)
    assert ServiceSettings.from_env().data_dir == Path("relative-development-data")
    assert not (tmp_path / "relative-development-data").exists()


def test_existing_database_is_selected_without_opening_or_modifying_it(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    root = Path(environment["NOYRA_DATA_DIR"])
    database = _database(root)
    before = database.read_bytes()
    assert managed_data_root(environ=environment) == root
    assert database.read_bytes() == before
    assert not (root / "noyra.sqlite3-wal").exists()


def test_missing_database_never_silently_creates_a_subject(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    with pytest.raises(ValueError, match="database missing"):
        managed_data_root(environ=environment)
    assert not Path(environment["NOYRA_DATA_DIR"]).exists()


@pytest.mark.parametrize(
    "name", ["NOYRA_DATA_DIR", "NOYRA_MANAGED_INSTALL_ROOT", "NOYRA_MANAGED_INITIALIZE"]
)
def test_incomplete_contract_fails_without_writes(tmp_path: Path, name: str) -> None:
    environment = _environment(tmp_path)
    environment.pop(name)
    with pytest.raises(ValueError, match="incomplete"):
        managed_data_root(environ=environment)
    assert not Path(environment["NOYRA_MANAGED_DATA_ROOT"]).exists()


@pytest.mark.parametrize("value", ["", "relative-data", ".", "../data"])
def test_managed_environment_cannot_revert_to_relative_paths(tmp_path: Path, value: str) -> None:
    environment = _environment(tmp_path, initialize=True)
    environment["NOYRA_DATA_DIR"] = value
    with pytest.raises(ValueError, match="managed_data_root"):
        managed_data_root(environ=environment)


def test_conflicting_environment_is_rejected_before_service_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path, initialize=True)
    environment["NOYRA_DATA_DIR"] = str(tmp_path / "wrong-data")
    _install_environment(monkeypatch, environment)
    with pytest.raises(ValueError, match="differs"):
        NoyraService.from_env()
    assert not (tmp_path / "wrong-data").exists()
    assert not Path(environment["NOYRA_MANAGED_DATA_ROOT"]).exists()


def test_direct_service_settings_cannot_override_managed_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "wrong-data", subject_id="Noyra-f01", genesis_hash=content_hash("f01")
    )
    _install_environment(monkeypatch, _environment(tmp_path, initialize=True))
    with pytest.raises(ValueError, match="differs"):
        NoyraService(settings)
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("nested", ["data-in-install", "install-in-data", "same-root"])
def test_managed_roots_must_be_disjoint(tmp_path: Path, nested: str) -> None:
    environment = _environment(tmp_path, initialize=True)
    if nested == "data-in-install":
        environment["NOYRA_DATA_DIR"] = str(
            Path(environment["NOYRA_MANAGED_INSTALL_ROOT"]) / "data"
        )
        environment["NOYRA_MANAGED_DATA_ROOT"] = environment["NOYRA_DATA_DIR"]
    elif nested == "install-in-data":
        environment["NOYRA_MANAGED_INSTALL_ROOT"] = str(
            Path(environment["NOYRA_DATA_DIR"]) / "install"
        )
    else:
        environment["NOYRA_MANAGED_INSTALL_ROOT"] = environment["NOYRA_DATA_DIR"]
    with pytest.raises(ValueError, match="disjoint"):
        managed_data_root(environ=environment)


def test_explicit_initialization_only_accepts_a_directory_without_subject_data(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, initialize=True)
    root = Path(environment["NOYRA_DATA_DIR"])
    assert managed_data_root(environ=environment) == root
    assert not root.exists()
    root.mkdir()
    (root / ".noyra-lifecycle.lock").touch()
    (root / "noyra.pid").write_text("parent-published-process-identity", encoding="utf-8")
    assert managed_data_root(environ=environment) == root
    (root / "secrets").mkdir()
    with pytest.raises(ValueError, match="without data"):
        managed_data_root(environ=environment)
    assert not (root / "noyra.sqlite3").exists()


def test_initialization_does_not_reinterpret_an_existing_database(tmp_path: Path) -> None:
    environment = _environment(tmp_path, initialize=True)
    database = _database(Path(environment["NOYRA_DATA_DIR"]))
    before = database.read_bytes()
    with pytest.raises(ValueError, match="existing database"):
        managed_data_root(environ=environment)
    assert database.read_bytes() == before


@pytest.mark.parametrize("kind", ["zero-byte", "directory", "wal-only"])
def test_broken_or_partial_database_is_never_reinitialized(tmp_path: Path, kind: str) -> None:
    environment = _environment(tmp_path, initialize=True)
    root = Path(environment["NOYRA_DATA_DIR"])
    root.mkdir()
    database = root / "noyra.sqlite3"
    if kind == "zero-byte":
        database.touch()
    elif kind == "directory":
        database.mkdir()
    else:
        (root / "noyra.sqlite3-wal").write_bytes(b"uncheckpointed-fixture")
    with pytest.raises(ValueError, match="managed_data_root"):
        managed_data_root(environ=environment)


def _powershell(script: Path, *arguments: str) -> dict[str, Any]:
    assert POWERSHELL is not None
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    value: dict[str, Any] = json.loads(completed.stdout)
    return value


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell 7 is unavailable")
def test_real_child_receives_only_its_instance_data_root_and_parent_environment_is_unchanged(
    tmp_path: Path,
) -> None:
    script = tmp_path / "child-environment.ps1"
    script.write_text(
        r"""
param([string]$Helper,[string]$Root,[string]$Python,[string]$Reporter)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $Helper
$InstallRoot=Join-Path $Root 'installed versions'
$DataRoot=Join-Path $Root 'retained data 数据'
New-Item -ItemType Directory -Path $InstallRoot,$DataRoot | Out-Null
[IO.File]::WriteAllBytes((Join-Path $DataRoot 'noyra.sqlite3'),[byte[]](1,2,3))
$env:NOYRA_DATA_DIR='parent-data-must-remain-unchanged'
$env:NOYRA_MANAGED_INSTALL_ROOT='parent-install-must-remain-unchanged'
$env:NOYRA_MANAGED_DATA_ROOT='parent-managed-must-remain-unchanged'
$env:NOYRA_MANAGED_INITIALIZE='parent-init-must-remain-unchanged'
$children=@()
foreach ($version in @('v1','v2','v1')) {
    $working=Join-Path $InstallRoot ('versions/'+$version)
    New-Item -ItemType Directory -Path $working -Force | Out-Null
    $info=New-NoyraProcessStartInfo $Python $working $false
    if ($info.UseShellExecute -or -not $info.CreateNoWindow -or
        $info.WindowStyle -ne 'Hidden') { throw 'Unsafe child process flags.' }
    $info.ArgumentList.Add($Reporter)
    $info.RedirectStandardOutput=$true
    $info.RedirectStandardError=$true
    $process=[Diagnostics.Process]::new();$process.StartInfo=$info
    try {
        $null=$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill();throw 'Fixture child did not exit.'
        }
        if ($process.ExitCode -ne 0) { throw $stderr.GetAwaiter().GetResult() }
        $children+=($stdout.GetAwaiter().GetResult() | ConvertFrom-Json)
    }
    finally { $process.Dispose() }
}
$DataRoot=Join-Path $Root 'second independent instance'
$second=New-NoyraProcessStartInfo $Python $InstallRoot $true
[pscustomobject]@{
 children=$children;second_data=$second.EnvironmentVariables['NOYRA_DATA_DIR']
 second_initialization=$second.EnvironmentVariables['NOYRA_MANAGED_INITIALIZE']
 parent_data=$env:NOYRA_DATA_DIR;parent_install=$env:NOYRA_MANAGED_INSTALL_ROOT
 parent_managed=$env:NOYRA_MANAGED_DATA_ROOT;parent_initialization=$env:NOYRA_MANAGED_INITIALIZE
} | ConvertTo-Json -Depth 6 -Compress
""",
        encoding="utf-8",
    )
    reporter = tmp_path / "reporter.py"
    reporter.write_text(
        "import json,os\n"
        "print(json.dumps({k:os.environ[k] for k in "
        "['NOYRA_DATA_DIR','NOYRA_MANAGED_INSTALL_ROOT','NOYRA_MANAGED_DATA_ROOT',"
        "'NOYRA_MANAGED_INITIALIZE']},ensure_ascii=True))\n",
        encoding="utf-8",
    )
    result = _powershell(
        script,
        "-Helper",
        str(ROOT / "deploy/windows/noyra-data-root.ps1"),
        "-Root",
        str(tmp_path),
        "-Python",
        sys.executable,
        "-Reporter",
        str(reporter),
    )
    expected = str(tmp_path / "retained data 数据")
    assert len(result["children"]) == 3
    for child in result["children"]:
        assert child["NOYRA_DATA_DIR"] == child["NOYRA_MANAGED_DATA_ROOT"] == expected
        assert child["NOYRA_MANAGED_INITIALIZE"] == "0"
        assert child["NOYRA_MANAGED_INSTALL_ROOT"] == str(tmp_path / "installed versions")
    assert result["second_data"] == str(tmp_path / "second independent instance")
    assert result["second_initialization"] == "1"
    assert result["parent_data"] == "parent-data-must-remain-unchanged"
    assert result["parent_install"] == "parent-install-must-remain-unchanged"
    assert result["parent_managed"] == "parent-managed-must-remain-unchanged"
    assert result["parent_initialization"] == "parent-init-must-remain-unchanged"


@pytest.fixture
def windows_install(tmp_path: Path) -> dict[str, Path]:
    if os.name != "nt" or POWERSHELL is None:
        pytest.skip("native Windows lifecycle entry requires PowerShell 7")
    import hashlib

    packages = tmp_path / "packages"
    for version in ("v1", "v2", "v3"):
        package = packages / version
        package.mkdir(parents=True)
        content = b"synthetic package fixture; never executed"
        (package / "noyra.exe").write_bytes(content)
        (package / "noyra-package.json").write_text(
            json.dumps(
                {
                    "format": "noyra-windows-package/v1",
                    "version": version,
                    "launcher": "noyra.exe",
                    "signature_required": False,
                    "trusted_publisher_thumbprints": [],
                    "files": [{"path": "noyra.exe", "sha256": hashlib.sha256(content).hexdigest()}],
                }
            ),
            encoding="utf-8",
        )
    values = {"install": tmp_path / "install", "data": tmp_path / "retained", "packages": packages}
    for version in ("v1", "v2"):
        result = _lifecycle(values, "install", "-PackageRoot", str(packages / version))
        assert result.returncode == 0, result.stdout + result.stderr
    return values


def _lifecycle(
    paths: dict[str, Path], action: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-Action",
            action,
            "-InstallRoot",
            str(paths["install"]),
            "-DataRoot",
            str(paths["data"]),
            *arguments,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {
        str(entry.relative_to(root)): None if entry.is_dir() else entry.read_bytes()
        for entry in root.rglob("*")
    }


@pytest.mark.parametrize(
    "action", ["start", "install", "upgrade", "rollback", "recover", "uninstall"]
)
def test_legacy_subject_data_blocks_mutating_product_entries_before_state_changes(
    windows_install: dict[str, Path], action: str
) -> None:
    install = windows_install["install"]
    legacy = install / "versions" / "v1" / ".runtime" / "data"
    database = _database(legacy)
    (legacy / "noyra.sqlite3-wal").write_bytes(b"synthetic retained WAL")
    (legacy / "secrets").mkdir()
    (legacy / "secrets" / "fixture.key").write_bytes(b"synthetic secret sentinel")
    before = _tree_bytes(install)
    extra = (
        ("-PackageRoot", str(windows_install["packages"] / "v3"))
        if action in {"install", "upgrade"}
        else ()
    )
    result = _lifecycle(windows_install, action, *extra)
    assert result.returncode != 0
    assert "legacy-data-detected" in result.stderr
    assert _tree_bytes(install) == before
    assert database.exists()
    assert not (windows_install["data"] / "noyra.sqlite3").exists()


@pytest.mark.parametrize(
    "marker", ["empty-runtime", "orphan-wal", "loose-database", "hidden-runtime"]
)
def test_uninstall_cannot_delete_runtime_markers_in_unselected_or_hidden_locations(
    windows_install: dict[str, Path], marker: str
) -> None:
    install = windows_install["install"]
    base = install / ".unselected-hidden-directory"
    base.mkdir()
    if marker in {"empty-runtime", "hidden-runtime"}:
        legacy = base / ".runtime"
        legacy.mkdir()
        if marker == "hidden-runtime":
            # NTFS hidden metadata must not make this directory invisible to inspection.
            completed = subprocess.run(
                ["attrib", "+h", str(legacy)], capture_output=True, check=False, timeout=10
            )
            assert completed.returncode == 0
    else:
        (base / ("noyra.sqlite3-wal" if marker == "orphan-wal" else "noyra.sqlite3")).write_bytes(
            b"retain this synthetic data"
        )
    before = _tree_bytes(install)
    result = _lifecycle(windows_install, "uninstall")
    assert result.returncode != 0 and "legacy-data-detected" in result.stderr
    assert _tree_bytes(install) == before


def test_legacy_data_diagnostics_and_stop_remain_available(
    windows_install: dict[str, Path],
) -> None:
    legacy = windows_install["install"] / "versions" / "v1" / ".runtime" / "data"
    database = _database(legacy)
    before = database.read_bytes()
    result = _lifecycle(windows_install, "status")
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads(result.stdout)
    assert status["data_root_status"] == "legacy-data-detected"
    assert str(legacy.parent) in status["legacy_data_paths"]
    result = _lifecycle(windows_install, "stop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert database.read_bytes() == before
    # Explicit creation of a new subject cannot waive review of the old one.
    result = _lifecycle(windows_install, "start", "-InitializeDataRoot")
    assert result.returncode != 0 and "legacy-data-detected" in result.stderr


def test_missing_database_start_does_not_mutate_authority_or_initialize(
    windows_install: dict[str, Path],
) -> None:
    before = _tree_bytes(windows_install["install"])
    result = _lifecycle(windows_install, "start")
    assert result.returncode != 0 and "database-missing" in result.stderr
    assert _tree_bytes(windows_install["install"]) == before
    assert not (windows_install["data"] / "noyra.sqlite3").exists()
    status = json.loads(_lifecycle(windows_install, "status").stdout)
    assert status["data_root_status"] == "database-missing"


def test_upgrade_rollback_and_uninstall_preserve_the_actual_retained_database(
    windows_install: dict[str, Path],
) -> None:
    database = _database(windows_install["data"])
    before = _tree_bytes(windows_install["data"])
    result = _lifecycle(
        windows_install, "upgrade", "-PackageRoot", str(windows_install["packages"] / "v3")
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = _lifecycle(windows_install, "rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (windows_install["install"] / "current.txt").read_text(encoding="utf-8").strip() == "v2"
    status = json.loads(_lifecycle(windows_install, "status").stdout)
    assert status["data_root_status"] == "ready"
    result = _lifecycle(windows_install, "uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not windows_install["install"].exists()
    assert database.exists()
    assert _tree_bytes(windows_install["data"]) == before


def _junction(link: Path, target: Path, fixture_root: Path) -> None:
    script = fixture_root / "create-fixture-junction.ps1"
    script.write_text(
        "param([string]$Link,[string]$Target)\n"
        "$ErrorActionPreference='Stop'\n"
        "New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n"
        "'{}'\n",
        encoding="utf-8",
    )
    _powershell(script, "-Link", str(link), "-Target", str(target))


def test_installation_junction_does_not_allow_deleting_external_data(
    windows_install: dict[str, Path], tmp_path: Path
) -> None:
    target = tmp_path / "outside retained subject"
    database = _database(target)
    link = windows_install["install"] / "unselected-link"
    _junction(link, target, tmp_path)
    before = database.read_bytes()
    result = _lifecycle(windows_install, "uninstall")
    assert result.returncode != 0 and "unsafe-install-tree" in result.stderr
    assert windows_install["install"].exists() and database.read_bytes() == before
    status = json.loads(_lifecycle(windows_install, "status").stdout)
    assert status["data_root_status"] == "unsafe-install-tree"


def test_ancestor_junction_is_rejected_by_lifecycle_and_runtime(
    windows_install: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-parent"
    _junction(link, outside, tmp_path)
    paths = {**windows_install, "data": link / "retained"}
    result = _lifecycle(paths, "start", "-InitializeDataRoot")
    assert result.returncode != 0 and "unsafe-path" in result.stderr
    assert not (outside / "retained").exists()
    environment = _environment(tmp_path, initialize=True)
    environment["NOYRA_DATA_DIR"] = environment["NOYRA_MANAGED_DATA_ROOT"] = str(paths["data"])
    with pytest.raises(ValueError, match="filesystem link"):
        managed_data_root(environ=environment)
    assert not (outside / "retained").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
@pytest.mark.parametrize("suffix", [".", " "])
def test_windows_alias_paths_cannot_bypass_root_separation(tmp_path: Path, suffix: str) -> None:
    environment = _environment(tmp_path, initialize=True)
    environment["NOYRA_DATA_DIR"] = environment["NOYRA_MANAGED_DATA_ROOT"] = (
        environment["NOYRA_MANAGED_INSTALL_ROOT"] + suffix
    )
    with pytest.raises(ValueError, match="ambiguous Windows path"):
        managed_data_root(environ=environment)


def test_managed_service_reopens_the_same_subject_across_version_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path, initialize=True)
    environment.update(
        {
            "NOYRA_SUBJECT_ID": "Noyra-f01-continuity",
            "NOYRA_GENESIS_HASH": content_hash({"fixture": "f01-continuity"}),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PORT": "0",
            "NOYRA_COGNITION_ENABLED": "false",
            "NOYRA_STORAGE_PROFILE": "runtime_only",
            "NOYRA_AT_REST_MODE": "development",
        }
    )
    _install_environment(monkeypatch, environment)
    identities = []
    root = Path(environment["NOYRA_DATA_DIR"])
    for index, version in enumerate(("v1", "v2", "v1")):
        working = Path(environment["NOYRA_MANAGED_INSTALL_ROOT"]) / "versions" / version
        working.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(working)
        monkeypatch.setenv("NOYRA_MANAGED_INITIALIZE", "1" if index == 0 else "0")
        service = NoyraService.from_env()
        try:
            assert service.kernel.database.path == root / "noyra.sqlite3"
            with service.kernel.database.connection() as connection:
                rows = connection.execute("SELECT * FROM subject_identity").fetchall()
                assert len(rows) == 1
                identities.append(dict(rows[0]))
            service.kernel.event_store.verify_chain(service.kernel.subject_id)
        finally:
            service.close()
        assert not (working / ".runtime").exists()
    assert identities[0] == identities[1] == identities[2]
    # Losing the managed database cannot quietly create another identity.
    saved = root / "saved-fixture.sqlite3"
    (root / "noyra.sqlite3").rename(saved)
    with pytest.raises(ValueError, match="database missing"):
        NoyraService.from_env()
    assert not (root / "noyra.sqlite3").exists()
    assert saved.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows lifecycle")
@pytest.mark.parametrize("placement", ["data-in-install", "install-in-data", "same-root", "alias"])
def test_product_entry_rejects_overlapping_or_aliased_roots_before_creation(
    tmp_path: Path, placement: str
) -> None:
    if POWERSHELL is None:
        pytest.skip("PowerShell 7 unavailable")
    install, data = tmp_path / "installation", tmp_path / "retained"
    if placement == "data-in-install":
        data = install / "data"
    elif placement == "install-in-data":
        install = data / "installation"
    elif placement == "same-root":
        data = install
    else:
        data = Path(str(install) + ".")
    result = _lifecycle({"install": install, "data": data}, "start", "-InitializeDataRoot")
    assert result.returncode != 0
    assert "overlapping-roots" in result.stderr or "unsafe-path" in result.stderr
    assert not install.exists() and not data.exists()


def test_uninstall_rechecks_for_data_created_during_stop(
    windows_install: dict[str, Path], tmp_path: Path
) -> None:
    script = tmp_path / "late-data.ps1"
    script.write_text(
        r"""
param([string]$Lifecycle,[string]$Install,[string]$Data)
$ErrorActionPreference='Stop'
$global:F01FixtureInjected=Join-Path $Install 'late/.runtime'
$breakpoint=Set-PSBreakpoint -Script $Lifecycle -Command 'Stop-Noyra' -Action {
    [IO.Directory]::CreateDirectory($global:F01FixtureInjected) | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $global:F01FixtureInjected 'sentinel'),[byte[]](1,2,3))
}
$failure=$null
try { & $Lifecycle -Action uninstall -InstallRoot $Install -DataRoot $Data }
catch { $failure=$_.Exception.Message }
finally { Remove-PSBreakpoint -Breakpoint $breakpoint }
[pscustomobject]@{failure=$failure;data_retained=([IO.File]::Exists(
    (Join-Path $global:F01FixtureInjected 'sentinel')))} | ConvertTo-Json -Compress
""",
        encoding="utf-8",
    )
    result = _powershell(
        script,
        "-Lifecycle",
        str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
        "-Install",
        str(windows_install["install"]),
        "-Data",
        str(windows_install["data"]),
    )
    assert "legacy-data-detected" in result["failure"]
    assert result["data_retained"]
    assert windows_install["install"].exists()
