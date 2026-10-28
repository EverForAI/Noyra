param([string]$Lifecycle,[string]$Root,[string]$Data,[string]$Action,[string]$Version='',
    [string]$Pins='', [string]$Transaction='txn-1', [long]$BaseGeneration=0,
    [string]$Fault='',[switch]$Crash,[switch]$Development)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'windows-r03-protocol-prototype.ps1')
Import-R03LifecycleHelpers $Lifecycle
Initialize-R03Probe $Root $Data
$values = if ($Pins) { @($Pins.Split(',')) } else { @() }
try {
    $result = Invoke-R03Probe -Action $Action -Version $Version -Pins $values -Strict:(-not $Development) -Transaction $Transaction -BaseGeneration $BaseGeneration -Fault $Fault -Crash:$Crash
    $result | ConvertTo-Json -Depth 10 -Compress
}
catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
