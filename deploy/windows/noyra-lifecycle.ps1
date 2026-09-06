[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'upgrade', 'rollback', 'status')]
    [string]$Action,
    [Parameter(Mandatory = $false)]
    [string]$InstallRoot = "$env:ProgramFiles\Noyra",
    [Parameter(Mandatory = $false)]
    [string]$DataRoot = "$env:ProgramData\Noyra",
    [Parameter(Mandatory = $false)]
    [string]$PackageRoot,
    [Parameter(Mandatory = $false)]
    [string]$Version,
    [Parameter(Mandatory = $false)]
    [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$versions = Join-Path $InstallRoot 'versions'
$currentPointer = Join-Path $InstallRoot 'current.txt'
$previousPointer = Join-Path $InstallRoot 'previous.txt'
$pidFile = Join-Path $DataRoot 'noyra.pid'

function Assert-SafeSegment([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw "$Name is not a safe package/version segment."
    }
}

function Read-Pointer([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $value = (Get-Content -LiteralPath $Path -Raw).Trim()
    if ($null -eq $value -or $value -eq '') { return $null }
    Assert-SafeSegment $value 'pointer'
    return $value
}

function Write-PointerAtomic([string]$Path, [string]$Value) {
    Assert-SafeSegment $Value 'pointer'
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Set-Content -LiteralPath $temporary -Value $Value -NoNewline -Encoding utf8
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Verify-Package([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "Package root is missing: $Root" }
    $manifestPath = Join-Path $Root 'noyra-package.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Package manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format -ne 'noyra-windows-package/v1') { throw 'Unsupported package manifest.' }
    Assert-SafeSegment ([string]$manifest.version) 'manifest version'
    $manifestRequiresSignature = $false
    $signatureProperty = $manifest.PSObject.Properties['signature_required']
    if ($null -ne $signatureProperty) { $manifestRequiresSignature = [bool]$signatureProperty.Value }
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $seen = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative) -or
            $relative.Contains('..') -or $relative.Contains('\') -or $seen.ContainsKey($relative)) {
            throw 'Package manifest contains an unsafe path.'
        }
        $seen[$relative] = $true
        $file = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Package file is missing: $relative" }
        $resolvedFile = (Resolve-Path -LiteralPath $file).Path
        if (-not $resolvedFile.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Package file escapes package root: $relative"
        }
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Package hash mismatch: $relative" }
        $extension = [IO.Path]::GetExtension($file).ToLowerInvariant()
        if (($RequireSignature -or $manifestRequiresSignature) -and $extension -in @('.exe', '.msi', '.msix', '.appx')) {
            $signature = Get-AuthenticodeSignature -LiteralPath $file
            if ($signature.Status -ne 'Valid') { throw "Authenticode signature is not valid: $relative" }
        }
    }
    return $manifest
}

function Get-CurrentVersion { return Read-Pointer $currentPointer }

function Start-Noyra {
    $current = Get-CurrentVersion
    if ($null -eq $current) { throw 'No installed Noyra version is selected.' }
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $versionRoot = Join-Path $versions $current
    $launcher = Join-Path $versionRoot 'noyra.exe'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        $launcher = Join-Path $versionRoot 'noyra-desktop.exe'
    }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Installed package has no launcher: $current"
    }
    $process = Start-Process -FilePath $launcher -WorkingDirectory $versionRoot -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline -Encoding ascii
}

function Stop-Noyra {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return }
    $pidValue = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -ne $process) { Stop-Process -Id $pidValue -Force }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

switch ($Action) {
    'install' {
        if ([string]::IsNullOrWhiteSpace($PackageRoot)) { throw '-PackageRoot is required for install.' }
        $manifest = Verify-Package (Resolve-Path -LiteralPath $PackageRoot).Path
        $version = [string]$manifest.version
        $target = Join-Path $versions $version
        if (Test-Path -LiteralPath $target) { throw "Version already installed: $version" }
        New-Item -ItemType Directory -Force -Path $versions, $DataRoot | Out-Null
        $staging = Join-Path $versions ('.staging-' + [guid]::NewGuid().ToString('N'))
        try {
            $packagePath = (Resolve-Path -LiteralPath $PackageRoot).Path
            Get-ChildItem -LiteralPath $packagePath -File -Recurse | ForEach-Object {
                $relative = $_.FullName.Substring($packagePath.Length + 1)
                $destinationPath = Join-Path $staging $relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $destinationPath -Force
            }
            Verify-Package $staging | Out-Null
            Move-Item -LiteralPath $staging -Destination $target
            $old = Get-CurrentVersion
            if ($null -ne $old) { Write-PointerAtomic $previousPointer $old }
            Write-PointerAtomic $currentPointer $version
        }
        finally {
            if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
        }
    }
    'upgrade' {
        if ([string]::IsNullOrWhiteSpace($PackageRoot)) { throw '-PackageRoot is required for upgrade.' }
        & $PSCommandPath -Action install -InstallRoot $InstallRoot -DataRoot $DataRoot -PackageRoot $PackageRoot -RequireSignature:$RequireSignature
    }
    'rollback' {
        $previous = Read-Pointer $previousPointer
        if ($null -eq $previous -or -not (Test-Path -LiteralPath (Join-Path $versions $previous))) { throw 'No usable previous version is available.' }
        Verify-Package (Join-Path $versions $previous) | Out-Null
        $current = Get-CurrentVersion
        if ($null -ne $current) { Write-PointerAtomic $previousPointer $current }
        Write-PointerAtomic $currentPointer $previous
    }
    'start' { Start-Noyra }
    'stop' { Stop-Noyra }
    'uninstall' {
        Stop-Noyra
        if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
        # DataRoot is intentionally retained; deleting subject data requires a separate explicit action.
    }
    'status' {
        [pscustomobject]@{
            install_root = $InstallRoot
            data_root = $DataRoot
            current_version = Get-CurrentVersion
            previous_version = Read-Pointer $previousPointer
            running = (Test-Path -LiteralPath $pidFile -PathType Leaf)
        } | ConvertTo-Json -Compress
    }
}
