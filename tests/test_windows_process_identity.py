from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    "scenario",
    [
        "matching",
        "reused-pid",
        "wrong-path",
        "missing",
        "legacy",
        "invalid",
        "duplicate-start",
        "forced",
        "v1-stoppable",
        "v1-start-blocked",
        "wrong-data-root",
    ],
)
def test_windows_lifecycle_never_controls_unverified_pid(tmp_path: Path, scenario: str) -> None:
    assert POWERSHELL is not None
    script = tmp_path / "process-probe.ps1"
    script.write_text(
        r"""
param([string]$Source, [string]$Root, [string]$Scenario)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$versions = Join-Path $Root 'install/versions'
$DataRoot = Join-Path $Root 'data'
$pidFile = Join-Path $DataRoot 'noyra.pid'
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Source parse failed.' }
$functions = @('Assert-SafeSegment', 'Read-NoyraProcessIdentity', 'Get-TrackedNoyraProcess',
               'Write-NoyraProcessIdentity', 'Start-Noyra', 'Stop-Noyra')
foreach ($name in $functions) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    if ($null -eq $definition) { throw "Missing function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$launcher = [IO.Path]::GetFullPath((Join-Path $versions 'v1/noyra.exe'))
$start = [datetime]'2026-10-22T00:00:00Z'
$script:killCount = 0
$script:closeCount = 0
$script:lookupCount = 0
$script:disposeCount = 0
$script:fakeProcess = [pscustomobject]@{
    Id = 2147483000; Handle = 123; MainModule = [pscustomobject]@{ FileName = $launcher }
    StartTime = $start; HasExited = $false
}
$script:fakeProcess | Add-Member -MemberType ScriptMethod -Name Dispose -Value {
    $script:disposeCount++
}
$script:fakeProcess | Add-Member -MemberType ScriptMethod -Name CloseMainWindow -Value {
    $script:closeCount++
    return $true
}
$script:fakeProcess | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
    param([int]$Milliseconds)
    if ($Scenario -eq 'forced' -and $script:killCount -eq 0) { return $false }
    return $true
}
$script:fakeProcess | Add-Member -MemberType ScriptMethod -Name Kill -Value { $script:killCount++ }
function Get-Process {
    [CmdletBinding()] param([int]$Id)
    $script:lookupCount++
    if ($Scenario -eq 'missing') {
        throw [Microsoft.PowerShell.Commands.ProcessCommandException]::new('Fixture missing')
    }
    return $script:fakeProcess
}
function Start-Process { throw 'An already-running instance must not be started again.' }
Write-NoyraProcessIdentity $script:fakeProcess $launcher 'v1'
if ($Scenario -in @('v1-stoppable','v1-start-blocked','wrong-data-root')) {
    $record=Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    if ($Scenario -eq 'wrong-data-root') {
        $record.data_root=Join-Path $Root 'other-data'
    }
    else {
        $record.format='noyra-process/v1'
        $record.PSObject.Properties.Remove('data_root')
    }
    $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $pidFile -Encoding utf8
}

if ($Scenario -eq 'reused-pid') { $script:fakeProcess.StartTime = $start.AddMinutes(1) }
if ($Scenario -eq 'wrong-path') {
    $script:fakeProcess.MainModule.FileName = Join-Path $Root 'other.exe'
}
if ($Scenario -eq 'legacy') { Set-Content -LiteralPath $pidFile -Value '2147483000' }
if ($Scenario -eq 'invalid') {
    Set-Content -LiteralPath $pidFile -Value '{"process_id":2147483000}'
}
$failure = $null
try {
    if ($Scenario -in @('duplicate-start','v1-start-blocked')) { Start-Noyra }
    else { Stop-Noyra }
}
catch { $failure = $_.Exception.Message }
[pscustomobject]@{
    kills = $script:killCount; closes = $script:closeCount
    lookups = $script:lookupCount; disposals = $script:disposeCount
    identity_retained = (Test-Path -LiteralPath $pidFile); failure = $failure
    real_process_apis_called = $false
} | ConvertTo-Json -Compress
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Source",
            str(ROOT / "deploy/windows/noyra-lifecycle.ps1"),
            "-Root",
            str(tmp_path),
            "-Scenario",
            scenario,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert not data["real_process_apis_called"]
    if scenario in {"legacy", "invalid", "wrong-data-root"}:
        assert data["failure"]
        assert data["identity_retained"]
        assert data["lookups"] == data["kills"] == data["closes"] == 0
    elif scenario == "v1-start-blocked":
        assert "legacy-running-process" in data["failure"]
        assert data["identity_retained"]
        assert data["lookups"] == 1
        assert data["kills"] == data["closes"] == 0
    else:
        assert data["failure"] is None
        assert data["kills"] == (1 if scenario == "forced" else 0)
        assert data["closes"] == (1 if scenario in {"matching", "forced", "v1-stoppable"} else 0)
        assert data["identity_retained"] == (scenario == "duplicate-start")
