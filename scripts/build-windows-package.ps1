[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Version,
    [Parameter(Mandatory = $true)] [string]$OutputRoot,
    [Parameter(Mandatory = $false)] [string]$SourceRoot = (Join-Path $PSScriptRoot '..'),
    [Parameter(Mandatory = $false)] [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Stop-NoyraPackagePrerequisite([string]$Code, [string]$Detail) {
    [Console]::Error.WriteLine("NOYRA-WINDOWS-PACKAGE-PREREQUISITE[$Code]: $Detail")
    exit 78
}

function Assert-NoyraPackagePrerequisites {
    # These cmdlets are supplied by the standard PowerShell modules.  A
    # partially bundled pwsh (for example, one without the Management module)
    # otherwise fails at the first filesystem operation with an opaque command
    # loader error and can be mistaken for a package-content failure.
    try {
        $management = @(Get-Module -ListAvailable -Name Microsoft.PowerShell.Management)
    }
    catch {
        Stop-NoyraPackagePrerequisite 'PS-MANAGEMENT-MODULE' 'Microsoft.PowerShell.Management could not be discovered. Install or use a complete PowerShell 7 runtime.'
    }
    if ($management.Count -eq 0) {
        Stop-NoyraPackagePrerequisite 'PS-MANAGEMENT-MODULE' 'Microsoft.PowerShell.Management is missing. Install or use a complete PowerShell 7 runtime.'
    }
    try {
        Import-Module Microsoft.PowerShell.Management -ErrorAction Stop
    }
    catch {
        Stop-NoyraPackagePrerequisite 'PS-MANAGEMENT-MODULE' ("Microsoft.PowerShell.Management failed to load: " + $_.Exception.Message)
    }
    foreach ($command in @('Resolve-Path', 'Test-Path', 'Copy-Item', 'Get-FileHash')) {
        try {
            $available = Get-Command -Name $command -CommandType Cmdlet -ErrorAction SilentlyContinue
        }
        catch {
            $available = $null
        }
        if ($null -eq $available) {
            Stop-NoyraPackagePrerequisite 'PS-CMDLET' ("Required cmdlet '$command' is unavailable. Use a complete PowerShell 7 runtime.")
        }
    }
}

Assert-NoyraPackagePrerequisites
if ($Version -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw 'Version is not a safe package segment.' }
$source = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not (Test-Path -LiteralPath $OutputRoot)) {
    New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
}
$output = (Resolve-Path -LiteralPath $OutputRoot).Path
$destination = Join-Path $output $Version
if (Test-Path -LiteralPath $destination) { throw "Package destination already exists: $destination" }
$staging = Join-Path $output ('.staging-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    $files = @(
        'src',
        'deploy\windows\start-noyra.ps1',
        'deploy\windows\noyra-lifecycle.ps1',
        'README.md',
        'pyproject.toml',
        'requirements.lock',
        'requirements-cloud.lock'
    )
    foreach ($relative in $files) {
        $input = Join-Path $source $relative
        if (-not (Test-Path -LiteralPath $input)) { throw "Package input is missing: $relative" }
        $target = Join-Path $staging $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $input -Destination $target -Recurse -Force
    }
    $entries = @(
        Get-ChildItem -LiteralPath $staging -File -Recurse |
            Where-Object { $_.Name -ne 'noyra-package.json' } |
            ForEach-Object {
                [pscustomobject]@{
                    path = $_.FullName.Substring($staging.Length + 1).Replace('\', '/')
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
    )
    if ($RequireSignature) {
        $executables = Get-ChildItem -LiteralPath $staging -File -Recurse | Where-Object { $_.Extension -ieq '.exe' }
        foreach ($executable in $executables) {
            if ((Get-AuthenticodeSignature -LiteralPath $executable.FullName).Status -ne 'Valid') {
                throw "Executable is not Authenticode-signed: $($executable.FullName)"
            }
        }
    }
    $manifest = [ordered]@{
        format = 'noyra-windows-package/v1'
        version = $Version
        created_at = [DateTime]::UtcNow.ToString('o')
        files = @($entries | Sort-Object path)
        signature_required = [bool]$RequireSignature
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 6 -Compress
    Set-Content -LiteralPath (Join-Path $staging 'noyra-package.json') -Value $manifestJson -NoNewline -Encoding utf8
    Move-Item -LiteralPath $staging -Destination $destination
    Write-Output $destination
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
