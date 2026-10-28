param([string]$Lifecycle, [string]$WorkRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$results = [ordered]@{}
$names = @('Assert-SafeSegment', 'Assert-StateFileTarget', 'Publish-StateFile', 'Read-Pointer', 'Write-PointerAtomic', 'Normalize-Thumbprints', 'Read-PublisherTrustPolicy', 'Write-PublisherTrustPolicy', 'Write-InstalledVerificationPolicy')
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Lifecycle, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw 'Lifecycle script does not parse.' }
$definitions = @($ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names}, $false))
if ($definitions.Count -ne $names.Count) { throw 'Missing state publication helper.' }
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$InstallRoot = Join-Path $WorkRoot 'install'
[IO.Directory]::CreateDirectory($InstallRoot) | Out-Null
$publisherTrustPath = Join-Path $InstallRoot 'trusted-publishers.json'
function Expect-Rejected([string]$Name, [scriptblock]$Operation) {
    $rejected = $false
    try { & $Operation | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw "Operation unexpectedly succeeded: $Name" }
    $results[$Name] = $true
}
# Exercise the real script helpers without running a launcher or importing a cert.
$pointer = Join-Path $InstallRoot 'current.txt'
Write-PointerAtomic $pointer 'v1'
Write-PointerAtomic $pointer 'v2'
if ((Read-Pointer $pointer) -cne 'v2') { throw 'Normal file replacement failed.' }
$results.normal_create_and_replace = $true
$held = [IO.File]::Open($pointer, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try { Expect-Rejected 'locked_replace_reports_failure' { Write-PointerAtomic $pointer 'v3' } }
finally { $held.Dispose() }
if ((Read-Pointer $pointer) -cne 'v2' -or @(Get-ChildItem -LiteralPath $InstallRoot -Filter '*.tmp' -Force).Count -ne 0) {
    throw 'Failed replacement changed the selected value or leaked temporary state.'
}
$results.failed_replace_retains_original = $true
# A directory junction must not be followed by a state reader or writer.
$outside = Join-Path $WorkRoot 'outside'
[IO.Directory]::CreateDirectory($outside) | Out-Null
[IO.File]::WriteAllText((Join-Path $outside 'marker.txt'), 'retained')
$junction = Join-Path $InstallRoot 'previous.txt'
New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
Expect-Rejected 'junction_read_rejected' { Read-Pointer $junction }
Expect-Rejected 'junction_write_rejected' { Write-PointerAtomic $junction 'v3' }
if (@(Get-ChildItem -LiteralPath $outside -Force).Count -ne 1 -or
    [IO.File]::ReadAllText((Join-Path $outside 'marker.txt')) -cne 'retained') {
    throw 'State publication followed the junction.'
}
$results.junction_target_unchanged = $true
# Inject a directory after validation. The final primitive still must not treat
# it as a container (a preflight-only fix would fail this regression).
$original = ($definitions | Where-Object Name -eq 'Assert-StateFileTarget').Extent.Text
. ([scriptblock]::Create($original.Replace('function Assert-StateFileTarget(', 'function Assert-OriginalStateFileTarget(')))
$script:raceDestination = Join-Path $InstallRoot 'race.txt'
function Assert-StateFileTarget([string]$Path) {
    Assert-OriginalStateFileTarget $Path
    if ($Path -ceq $script:raceDestination) {
        [IO.Directory]::CreateDirectory($Path) | Out-Null
    }
}
$source = Join-Path $InstallRoot '.race.tmp'
[IO.File]::WriteAllText($source, 'v3')
Expect-Rejected 'directory_after_validation_rejected' { Publish-StateFile $source $script:raceDestination }
if (-not [IO.File]::Exists($source) -or @(Get-ChildItem -LiteralPath $script:raceDestination -Force).Count -ne 0) {
    throw 'Publication moved a file into a raced directory.'
}
$results.raced_directory_remains_empty = $true
$results | ConvertTo-Json -Compress
