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
    [switch]$RequireSignature,
    [Parameter(Mandatory = $false)]
    [string[]]$TrustedPublisherThumbprints
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ($InstallRoot -eq [IO.Path]::GetPathRoot($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) -or
    $DataRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $DataRoot.StartsWith($InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'InstallRoot must be a non-root directory separate from retained DataRoot.'
}
foreach ($path in @($InstallRoot, (Join-Path $InstallRoot 'versions'), $DataRoot)) {
    if ((Test-Path -LiteralPath $path) -and
        ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Installation and data roots must not use reparse points.'
    }
}
$versions = Join-Path $InstallRoot 'versions'
$currentPointer = Join-Path $InstallRoot 'current.txt'
$previousPointer = Join-Path $InstallRoot 'previous.txt'
$pidFile = Join-Path $DataRoot 'noyra.pid'

function Assert-SafeSegment([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw "$Name is not a safe package/version segment."
    }
}

function Normalize-Thumbprints([object[]]$Values) {
    if ($null -eq $Values -or $Values.Count -eq 0 -or
        ($Values.Count -eq 1 -and [string]::IsNullOrWhiteSpace([string]$Values[0]))) {
        return @()
    }
    return @($Values | ForEach-Object {
        $normalized = ([string]$_).Trim().Replace(' ', '').ToUpperInvariant()
        if ($normalized -notmatch '^[0-9A-F]{40}$') {
            throw 'Trusted publisher thumbprints must be 40 hexadecimal characters.'
        }
        $normalized
    } | Sort-Object -Unique)
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

function Assert-PackageTree([string]$Root) {
    # Enumerate one directory at a time: never descend into an unverified link.
    $ancestor = Get-Item -LiteralPath $Root -Force
    while ($null -ne $ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Package paths must not use reparse points.'
        }
        $ancestor = $ancestor.Parent
    }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        foreach ($item in @(Get-ChildItem -LiteralPath $pending.Pop() -Force)) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Package paths must not use reparse points.'
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else { $item }
        }
    }
}

function Verify-Package([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "Package root is missing: $Root" }
    $Root = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($Root -eq [IO.Path]::GetPathRoot($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)) {
        throw 'A filesystem root cannot be used as a package root.'
    }
    $inventory = @{}
    foreach ($file in @(Assert-PackageTree $Root)) {
        $relative = $file.FullName.Substring($Root.Length + 1).Replace('\', '/')
        if ($inventory.ContainsKey($relative)) { throw 'Package has case-colliding paths.' }
        $inventory[$relative] = $file.FullName
    }
    $manifestPath = Join-Path $Root 'noyra-package.json'
    if (-not $inventory.ContainsKey('noyra-package.json')) { throw 'Package manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format -ne 'noyra-windows-package/v1') { throw 'Unsupported package manifest.' }
    Assert-SafeSegment ([string]$manifest.version) 'manifest version'
    $launcherProperty = $manifest.PSObject.Properties['launcher']
    if ($null -eq $launcherProperty -or [string]$launcherProperty.Value -notin @('noyra.exe', 'noyra-desktop.exe')) {
        throw 'Package manifest must declare noyra.exe or noyra-desktop.exe as launcher.'
    }
    $manifestLauncher = [string]$launcherProperty.Value
    $manifestRequiresSignature = $false
    $signatureProperty = $manifest.PSObject.Properties['signature_required']
    if ($null -ne $signatureProperty) {
        if ($signatureProperty.Value -isnot [bool]) { throw 'Invalid signature requirement.' }
        $manifestRequiresSignature = $signatureProperty.Value
    }
    $manifestThumbprintProperty = $manifest.PSObject.Properties['trusted_publisher_thumbprints']
    $manifestThumbprintValues = if ($null -eq $manifestThumbprintProperty) { @() } else { @($manifestThumbprintProperty.Value) }
    $manifestThumbprints = Normalize-Thumbprints $manifestThumbprintValues
    $externalThumbprints = Normalize-Thumbprints $TrustedPublisherThumbprints
    $signatureRequired = $RequireSignature -or $manifestRequiresSignature
    if ($signatureRequired -and $externalThumbprints.Count -eq 0) {
        throw 'Signature verification requires an external trusted publisher thumbprint.'
    }
    if ($signatureRequired -and $manifestThumbprints.Count -gt 0 -and
        (@($manifestThumbprints) -join ',') -ne (@($externalThumbprints) -join ',')) {
        throw 'Package trusted publisher metadata does not match installer trust policy.'
    }
    $seen = @{'noyra-package.json' = $true}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Length -gt 4096 -or
            [IO.Path]::IsPathRooted($relative) -or $seen.ContainsKey($relative)) {
            throw 'Package manifest contains an unsafe path.'
        }
        foreach ($segment in $relative.Split('/')) {
            if ($segment -notmatch '^[^<>:"\\|?*\x00-\x1f/]+$' -or
                $segment -match '[. ]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)') {
                throw 'Package manifest contains an unsafe path.'
            }
        }
        $seen[$relative] = $true
        if (-not $inventory.ContainsKey($relative)) { throw "Package file is missing: $relative" }
        $file = $inventory[$relative]
        if ([string]$entry.sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'Invalid package hash.' }
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Package hash mismatch: $relative" }
        $extension = [IO.Path]::GetExtension($file).ToLowerInvariant()
        if ($signatureRequired -and $extension -in @('.exe', '.msi', '.msix', '.appx')) {
            $signature = Get-AuthenticodeSignature -LiteralPath $file
            if ($signature.Status -ne 'Valid' -or
                $externalThumbprints -notcontains $signature.SignerCertificate.Thumbprint.ToUpperInvariant()) {
                throw "Authenticode signature or trusted publisher is not valid: $relative"
            }
        }
    }
    if (-not $seen.ContainsKey($manifestLauncher)) { throw "Declared launcher is not in package inventory: $manifestLauncher" }
    foreach ($relative in $inventory.Keys) {
        if (-not $seen.ContainsKey($relative)) { throw "Unlisted package file: $relative" }
    }
    return $manifest
}

function Get-CurrentVersion { return Read-Pointer $currentPointer }

function Read-NoyraProcessIdentity {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $pidFile).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Process identity file must not be a reparse point.'
    }
    if ((Get-Item -LiteralPath $pidFile).Length -gt 8192) { throw 'Process identity file is too large.' }
    try {
        $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        if ($record.format -ne 'noyra-process/v1' -or [long]$record.process_id -le 0 -or
            [long]$record.process_id -gt [int]::MaxValue -or
            [string]$record.start_time_utc_ticks -notmatch '^[0-9]{1,19}$') { throw 'Invalid process identity.' }
        Assert-SafeSegment ([string]$record.version) 'process version'
        $versionRoot = [IO.Path]::GetFullPath((Join-Path $versions ([string]$record.version)))
        $path = [IO.Path]::GetFullPath([string]$record.executable)
        $allowed = @((Join-Path $versionRoot 'noyra.exe'), (Join-Path $versionRoot 'noyra-desktop.exe'))
        if ($path -notin $allowed) { throw 'Process executable is not an installed launcher.' }
        return $record
    }
    catch { throw 'Unverifiable process identity (including legacy PID-only files); refusing to control any process. Verify the instance manually.' }
}

function Get-TrackedNoyraProcess {
    $record = Read-NoyraProcessIdentity
    if ($null -eq $record) { return $null }
    try { $process = Get-Process -Id ([int]$record.process_id) -ErrorAction Stop }
    catch [Microsoft.PowerShell.Commands.ProcessCommandException] { return $null }
    try {
        # Pin the process handle; never verify then stop via another PID lookup.
        $null = $process.Handle
        $actualPath = [IO.Path]::GetFullPath($process.MainModule.FileName)
        $actualStart = $process.StartTime.ToUniversalTime().Ticks.ToString()
        if ($actualStart -ne [string]$record.start_time_utc_ticks -or
            -not $actualPath.Equals([string]$record.executable, [StringComparison]::OrdinalIgnoreCase)) {
            $process.Dispose()
            return $null
        }
        return $process
    }
    catch {
        $process.Dispose()
        throw 'Cannot verify the tracked process; refusing to stop or replace it.'
    }
}

function Write-NoyraProcessIdentity($Process, [string]$Launcher, [string]$Current) {
    $record = @{
        format = 'noyra-process/v1'
        process_id = $Process.Id
        start_time_utc_ticks = $Process.StartTime.ToUniversalTime().Ticks.ToString()
        executable = [IO.Path]::GetFullPath($Launcher)
        version = $Current
    }
    $temporary = Join-Path $DataRoot ('.noyra-process-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -NoNewline -Encoding utf8
        Move-Item -LiteralPath $temporary -Destination $pidFile -Force
    }
    finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Start-Noyra {
    $existing = Get-TrackedNoyraProcess
    if ($null -ne $existing) { $existing.Dispose(); return }
    $current = Get-CurrentVersion
    if ($null -eq $current) { throw 'No installed Noyra version is selected.' }
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $versionRoot = Join-Path $versions $current
    # Revalidate installed inventory and select only the manifest-declared launcher.
    $installedManifest = Verify-Package $versionRoot
    $launcher = Join-Path $versionRoot ([string]$installedManifest.launcher)
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Installed package has no launcher: $current"
    }
    foreach ($path in @($versionRoot, $launcher)) {
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Installed launchers must not use reparse points.'
        }
    }
    $process = Start-Process -FilePath $launcher -WorkingDirectory $versionRoot -WindowStyle Hidden -PassThru
    try { Write-NoyraProcessIdentity $process $launcher $current }
    catch {
        if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(5000) }
        throw
    }
    finally { $process.Dispose() }
}

function Stop-Noyra {
    $process = Get-TrackedNoyraProcess
    if ($null -ne $process) {
        try {
            if (-not $process.HasExited) {
                $null = $process.CloseMainWindow()
                if (-not $process.WaitForExit(5000)) {
                    $process.Kill()
                    if (-not $process.WaitForExit(5000)) { throw 'Noyra did not stop.' }
                }
            }
        }
        finally { $process.Dispose() }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# Serialize start/stop/uninstall, including identity-file publication.
$instanceLock = $null
if ($Action -in @('start', 'stop', 'uninstall')) {
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $instanceLock = [IO.File]::Open((Join-Path $DataRoot '.noyra-lifecycle.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
try {

switch ($Action) {
    'install' {
        if ([string]::IsNullOrWhiteSpace($PackageRoot)) { throw '-PackageRoot is required for install.' }
        $manifest = Verify-Package $PackageRoot
        if ($RequireSignature) {
            if ($null -eq $manifest.PSObject.Properties['signature_required']) {
                $manifest | Add-Member -NotePropertyName signature_required -NotePropertyValue $true
            } else {
                $manifest.signature_required = $true
            }
        }
        $version = [string]$manifest.version
        $target = Join-Path $versions $version
        if (Test-Path -LiteralPath $target) { throw "Version already installed: $version" }
        New-Item -ItemType Directory -Force -Path $versions, $DataRoot | Out-Null
        $staging = Join-Path $versions ('.staging-' + [guid]::NewGuid().ToString('N'))
        try {
            $packagePath = (Resolve-Path -LiteralPath $PackageRoot).Path
            New-Item -ItemType Directory -Path $staging | Out-Null
            foreach ($entry in @($manifest.files)) {
                $relative = [string]$entry.path
                $destinationPath = Join-Path $staging $relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
                Copy-Item -LiteralPath (Join-Path $packagePath $relative) -Destination $destinationPath -Force
            }
            # Pin the verified manifest rather than re-reading a mutable source.
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $staging 'noyra-package.json') -Encoding utf8
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
        & $PSCommandPath -Action install -InstallRoot $InstallRoot -DataRoot $DataRoot -PackageRoot $PackageRoot -RequireSignature:$RequireSignature -TrustedPublisherThumbprints $TrustedPublisherThumbprints
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
        $tracked = Get-TrackedNoyraProcess
        $running = $null -ne $tracked
        if ($null -ne $tracked) { $tracked.Dispose() }
        [pscustomobject]@{
            install_root = $InstallRoot
            data_root = $DataRoot
            current_version = Get-CurrentVersion
            previous_version = Read-Pointer $previousPointer
            running = $running
        } | ConvertTo-Json -Compress
    }
}

}
finally { if ($null -ne $instanceLock) { $instanceLock.Dispose() } }
