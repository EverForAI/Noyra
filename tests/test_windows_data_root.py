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
