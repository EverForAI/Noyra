[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'upgrade', 'rollback', 'recover', 'status')]
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
    [string[]]$TrustedPublisherThumbprints,
    [Parameter(Mandatory = $false)]
    [switch]$InitializeDataRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
. (Join-Path $PSScriptRoot 'noyra-data-root.ps1')
Assert-NoyraRootSeparation
if ($InitializeDataRoot -and $Action -ne 'start') { throw 'InitializeDataRoot is only valid for start.' }
$versions = Join-Path $InstallRoot 'versions'
$currentPointer = Join-Path $InstallRoot 'current.txt'
$previousPointer = Join-Path $InstallRoot 'previous.txt'
$publisherTrustPath = Join-Path $InstallRoot 'trusted-publishers.json'
$script:r03StatePath = Join-Path $InstallRoot 'r03-state.json'
$script:r03TransitionPath = Join-Path $InstallRoot 'r03-transition.json'
$script:r03MarkerPath = Join-Path $InstallRoot 'r03-protocol-required'
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

function Assert-StateFileTarget([string]$Path) {
    try { $attributes = [IO.File]::GetAttributes($Path) }
    catch [IO.FileNotFoundException] { return }
    catch [IO.DirectoryNotFoundException] { return }
    if (($attributes -band [IO.FileAttributes]::Directory) -or
        ($attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Installation state path must be a regular file: $Path"
    }
}

function Publish-StateFile([string]$Temporary, [string]$Path) {
    Assert-StateFileTarget $Path
    # Unlike Move-Item, these APIs never interpret a directory as a container.
    # Do not delete a selected state file first or fall back after a failed replace.
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($Temporary, $Path, [NullString]::Value)
    }
    else { [IO.File]::Move($Temporary, $Path) }
}

function Read-Pointer([string]$Path) {
    Assert-StateFileTarget $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $value = (Get-Content -LiteralPath $Path -Raw).Trim()
    if ($null -eq $value -or $value -eq '') { return $null }
    Assert-SafeSegment $value 'pointer'
    return $value
}

function Write-PointerAtomic([string]$Path, [string]$Value) {
    Assert-SafeSegment $Value 'pointer'
    Assert-StateFileTarget $Path
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Set-Content -LiteralPath $temporary -Value $Value -NoNewline -Encoding utf8
        Publish-StateFile $temporary $Path
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Read-PublisherTrustPolicy {
    Assert-StateFileTarget $publisherTrustPath
    if (-not (Test-Path -LiteralPath $publisherTrustPath -PathType Leaf)) { return @() }
    $item = Get-Item -LiteralPath $publisherTrustPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint -or $item.Length -gt 16KB) {
        throw 'Publisher trust policy is unsafe.'
    }
    $document = Get-Content -LiteralPath $publisherTrustPath -Raw | ConvertFrom-Json
    if ($document.format -ne 'noyra-publisher-trust/v1') {
        throw 'Publisher trust policy has an unsupported format.'
    }
    $values = if ($null -eq $document.thumbprints) { @() } else { @($document.thumbprints) }
    $normalized = @(Normalize-Thumbprints $values)
    if ($normalized.Count -eq 0) { throw 'Publisher trust policy is empty.' }
    return $normalized
}

function Write-PublisherTrustPolicy([string[]]$Thumbprints) {
    $normalized = @(Normalize-Thumbprints $Thumbprints)
    if ($normalized.Count -eq 0) { throw 'Cannot persist an empty publisher trust policy.' }
    Assert-StateFileTarget $publisherTrustPath
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    $temporary = Join-Path $InstallRoot ('.trusted-publishers.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $document = [ordered]@{
            format = 'noyra-publisher-trust/v1'
            thumbprints = @($normalized)
        }
        $document | ConvertTo-Json -Depth 4 -Compress |
            Set-Content -LiteralPath $temporary -NoNewline -Encoding utf8
        Publish-StateFile $temporary $publisherTrustPath
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Write-InstalledVerificationPolicy([string]$InstalledVersion, [bool]$SignatureRequired) {
    Assert-SafeSegment $InstalledVersion 'verification policy version'
    $path = Join-Path $InstallRoot ("verification-policy.$InstalledVersion.json")
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Installed verification policy is unsafe.'
        }
    }
    $temporary = Join-Path $InstallRoot ('.verification-policy.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [ordered]@{
            format = 'noyra-installed-verification/v1'
            version = $InstalledVersion
            signature_required = $SignatureRequired
        } | ConvertTo-Json -Compress |
            Set-Content -LiteralPath $temporary -NoNewline -Encoding utf8
        Publish-StateFile $temporary $path
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Read-RecoverySignatureRequirement([string]$InstalledVersion) {
    if (-not [string]::IsNullOrWhiteSpace($InstalledVersion)) {
        Assert-SafeSegment $InstalledVersion 'verification policy version'
        $path = Join-Path $InstallRoot ("verification-policy.$InstalledVersion.json")
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint -or
                $item.Length -gt 16KB) {
                throw 'Installed verification policy is unsafe.'
            }
            $document = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            if ($document.format -ne 'noyra-installed-verification/v1' -or
                $document.version -cne $InstalledVersion -or
                $document.signature_required -isnot [bool]) {
                throw 'Installed verification policy is invalid.'
            }
            return [bool]$document.signature_required
        }
    }
    # Older strict installs persisted only the external publisher allowlist.
    # Missing receipts must not turn such an installation into unsigned recovery.
    return ($TrustedPublisherThumbprints.Count -gt 0 -or
        @(Read-PublisherTrustPolicy).Count -gt 0)
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

function Verify-ManifestSignature(
    [string]$Root,
    [byte[]]$ManifestBytes,
    [string[]]$TrustedThumbprints
) {
    $signaturePath = Join-Path $Root 'noyra-package.json.p7s'
    if (-not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
        throw 'Strict package verification requires noyra-package.json.p7s.'
    }
    if ((Get-Item -LiteralPath $signaturePath).Length -gt 1MB) {
        throw 'Package manifest signature exceeds its size limit.'
    }
    try {
        Add-Type -AssemblyName System.Security.Cryptography.Pkcs -ErrorAction Stop
        $content = [System.Security.Cryptography.Pkcs.ContentInfo]::new($ManifestBytes)
        $cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($content, $true)
        $cms.Decode([IO.File]::ReadAllBytes($signaturePath))
        $cms.CheckSignature($true)
    }
    catch {
        throw 'Package manifest signature is invalid.'
    }
    if ($cms.SignerInfos.Count -ne 1 -or $null -eq $cms.SignerInfos[0].Certificate) {
        throw 'Package manifest signature must have exactly one signer certificate.'
    }
    $signerThumbprint = $cms.SignerInfos[0].Certificate.Thumbprint.ToUpperInvariant()
    if ($TrustedThumbprints -notcontains $signerThumbprint) {
        throw 'Package manifest signer is not a trusted publisher.'
    }
}

function Verify-Package([string]$Root, [switch]$ForceSignature) {
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
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 16MB) {
        throw 'Package manifest exceeds its size limit.'
    }
    $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
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
    # PowerShell unwraps a one-item pipeline result to a scalar. Keep the
    # trust-policy contract array-shaped for zero, one, and many publishers.
    $manifestThumbprints = @(Normalize-Thumbprints $manifestThumbprintValues)
    $externalThumbprints = @(Normalize-Thumbprints $TrustedPublisherThumbprints)
    $signatureRequired = $RequireSignature -or $ForceSignature -or $manifestRequiresSignature
    if ($signatureRequired) {
        if ($externalThumbprints.Count -eq 0) {
            throw 'Signature verification requires an external trusted publisher thumbprint.'
        }
        if (-not $manifestRequiresSignature) {
            throw 'The signed manifest must require strict package verification.'
        }
        if ($manifestThumbprints.Count -eq 0) {
            throw 'Strict package verification requires trusted publisher metadata in the manifest.'
        }
        # Metadata describes this package's publishers; only the external
        # policy grants trust. Rotation may add or remove unused publishers.
        # Every actual CMS and executable signer must remain in both sets.
        $effectiveThumbprints = @($externalThumbprints | Where-Object {
            $manifestThumbprints -contains $_
        })
        if ($effectiveThumbprints.Count -eq 0) {
            throw 'Package has no publishers authorized by installer trust policy.'
        }
        Verify-ManifestSignature -Root $Root -ManifestBytes $manifestBytes `
            -TrustedThumbprints $effectiveThumbprints
    }
    $seen = @{'noyra-package.json' = $true}
    if ($signatureRequired) { $seen['noyra-package.json.p7s'] = $true }
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
                $effectiveThumbprints -notcontains $signature.SignerCertificate.Thumbprint.ToUpperInvariant()) {
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

. (Join-Path $PSScriptRoot 'noyra-r03-recovery.ps1')
$script:R03RequestedPins = @(Normalize-Thumbprints $TrustedPublisherThumbprints)
$TrustedPublisherThumbprints = @($script:R03RequestedPins)
$script:R03CurrentState = $null
$script:R03TransitionState = $null
$hasR03Files = Test-R03ProtocolFiles
if ($script:R03RequestedPins.Count -eq 0 -and -not $hasR03Files) {
    $TrustedPublisherThumbprints = @(Read-PublisherTrustPolicy)
}

function Get-CurrentVersion {
    if ($null -ne $script:R03CurrentState) {
        if ($script:R03CurrentState.phase -cne 'ready') { throw 'R03 recovery is required before selecting a version.' }
        return [string]$script:R03CurrentState.current
    }
    if (Test-R03ProtocolFiles) {
        $state = Read-R03State
        if ($state.phase -cne 'ready') { throw 'R03 recovery is required before selecting a version.' }
        return [string]$state.current
    }
    return Read-Pointer $currentPointer
}

function Read-NoyraProcessIdentity {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $pidFile).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Process identity file must not be a reparse point.'
    }
    if ((Get-Item -LiteralPath $pidFile).Length -gt 8192) { throw 'Process identity file is too large.' }
    try {
        $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        if ($record.format -notin @('noyra-process/v1', 'noyra-process/v2') -or [long]$record.process_id -le 0 -or
            [long]$record.process_id -gt [int]::MaxValue -or
            [string]$record.start_time_utc_ticks -notmatch '^[0-9]{1,19}$') { throw 'Invalid process identity.' }
        if ($record.format -eq 'noyra-process/v2' -and
            ($record.data_root -isnot [string] -or -not [IO.Path]::IsPathRooted($record.data_root) -or
            -not ([IO.Path]::GetFullPath($record.data_root)).Equals($DataRoot, [StringComparison]::OrdinalIgnoreCase))) {
            throw 'Tracked process data root does not match the lifecycle data root.'
        }
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
        format = 'noyra-process/v2'
        data_root = $DataRoot
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
    if ($null -ne $existing) {
        try {
            if ((Read-NoyraProcessIdentity).format -ne 'noyra-process/v2') {
                throw 'NOYRA-DATA-ROOT[legacy-running-process]: stop the verified legacy process and review its actual data location before restarting.'
            }
        }
        finally { $existing.Dispose() }
        return
    }
    $current = Get-CurrentVersion
    if ($null -eq $current) { throw 'No installed Noyra version is selected.' }
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $versionRoot = Join-Path $versions $current
    # Revalidate installed inventory and select only the manifest-declared launcher.
    $installedManifest = Verify-Package -Root $versionRoot -ForceSignature:([bool]($RequireSignature -or ($null -ne $script:R03CurrentState -and $script:R03CurrentState.strict)))
    $launcher = Join-Path $versionRoot ([string]$installedManifest.launcher)
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Installed package has no launcher: $current"
    }
    foreach ($path in @($versionRoot, $launcher)) {
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Installed launchers must not use reparse points.'
        }
    }
    $process = Start-NoyraBoundProcess $launcher $versionRoot ([bool]$InitializeDataRoot)
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

# Serialize installation, pointer changes, and process identity publication.
$instanceLock = $null
if ($Action -in @('install', 'rollback', 'start', 'stop', 'uninstall', 'recover')) {
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $instanceLock = [IO.File]::Open((Join-Path $DataRoot '.noyra-lifecycle.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
try {

# Resolve/repair authority only while holding the same lifecycle lock as pointer changes.
if ($Action -in @('install', 'rollback', 'start', 'recover')) {
    if ($Action -eq 'install' -and -not (Test-R03ProtocolFiles)) {
        # Verify and stage the requested package before creating adoption state.
        $script:R03CurrentState = $null
    }
    else { $script:R03CurrentState = Get-OrAdopt-R03State -RepairMarker:($Action -eq 'recover') }
    if ($null -ne $script:R03CurrentState) {
        if ($Action -eq 'install' -and $script:R03RequestedPins.Count -gt 0) {
            $script:TrustedPublisherThumbprints = @($script:R03RequestedPins)
        }
        else { $script:TrustedPublisherThumbprints = @($script:R03CurrentState.pins) }
        if ($script:R03CurrentState.phase -cne 'ready') {
            # A durable PREPARED record may outlive a failed marker publish. Keep
            # the protocol discoverable before completing that accepted intent.
            Write-R03Marker
            $script:R03CurrentState = Complete-R03Recovery $script:R03CurrentState
        }
        if ($script:R03RequestedPins.Count -gt 0 -and $Action -in @('start', 'rollback', 'recover')) {
            Set-R03VerificationPins @($script:R03CurrentState.pins) -AllowNarrowing
        }
    }
    elseif ($Action -eq 'recover') { throw 'No R03 transaction or legacy installation is available to recover.' }
}
elseif ($Action -eq 'status') {
    $script:R03CurrentState = Read-R03State
    $script:R03TransitionState = Read-R03Transition
    if ($null -eq $script:R03CurrentState -and $null -ne $script:R03TransitionState) {
        $script:R03CurrentState = $script:R03TransitionState.prior_state
    }
    elseif ($null -ne $script:R03CurrentState -and $null -ne $script:R03TransitionState) {
        if (Test-R03TransitionAccepted $script:R03CurrentState $script:R03TransitionState) {
            $script:R03TransitionState = $null
        }
        elseif ($script:R03CurrentState.phase -ceq 'ready') {
            $script:R03CurrentState = $script:R03TransitionState.prior_state
        }
        else { $script:R03TransitionState = $null }
    }
}
elseif ($Action -eq 'uninstall' -and (Test-R03ProtocolFiles)) {
    if ($null -ne (Read-R03Transition)) { throw 'R03 recovery must complete before uninstall.' }
    $script:R03CurrentState = Read-R03State
    if ($null -eq $script:R03CurrentState -or $script:R03CurrentState.phase -cne 'ready') {
        throw 'R03 recovery must complete before uninstall.'
    }
}

# Validate every shared publication target before changing receipts, trust or
# pointers. The writer rechecks immediately before its file-only atomic publish.
if ($Action -in @('install', 'rollback')) {
    foreach ($path in @($currentPointer, $previousPointer, $publisherTrustPath)) {
        Assert-StateFileTarget $path
    }
}

switch ($Action) {
    'install' {
        if ([string]::IsNullOrWhiteSpace($PackageRoot)) { throw '-PackageRoot is required for install.' }
        $manifest = Verify-Package -Root $PackageRoot
        $version = [string]$manifest.version
        $target = Join-Path $versions $version
        New-Item -ItemType Directory -Force -Path $versions, $DataRoot | Out-Null
        $staging = $null
        try {
            if (Test-Path -LiteralPath $target) {
                # An interrupted install may have promoted this directory before
                # publishing its receipt. Reuse it only after full verification
                # and exact manifest identity with the explicitly supplied source.
                $installedManifest = Verify-Package -Root $target -ForceSignature:([bool]$manifest.signature_required)
                $sourceManifestHash = (Get-FileHash -LiteralPath (Join-Path $PackageRoot 'noyra-package.json') -Algorithm SHA256).Hash
                $targetManifestHash = (Get-FileHash -LiteralPath (Join-Path $target 'noyra-package.json') -Algorithm SHA256).Hash
                if ([string]$installedManifest.version -cne $version -or
                    $sourceManifestHash -cne $targetManifestHash) {
                    throw 'Existing version differs from the supplied package; refusing to replace it.'
                }
            }
            else {
                $staging = Join-Path $versions ('.staging-' + [guid]::NewGuid().ToString('N'))
                $packagePath = (Resolve-Path -LiteralPath $PackageRoot).Path
                New-Item -ItemType Directory -Path $staging | Out-Null
                foreach ($entry in @($manifest.files)) {
                    $relative = [string]$entry.path
                    $destinationPath = Join-Path $staging $relative
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
                    Copy-Item -LiteralPath (Join-Path $packagePath $relative) -Destination $destinationPath -Force
                }
                if ($manifest.signature_required) {
                    Copy-Item -LiteralPath (Join-Path $packagePath 'noyra-package.json.p7s') `
                        -Destination (Join-Path $staging 'noyra-package.json.p7s') -Force
                }
                # Preserve the exact signed bytes. Re-serializing JSON changes the
                # detached-signature input even when the parsed object is equal.
                [IO.File]::WriteAllBytes(
                    (Join-Path $staging 'noyra-package.json'),
                    [IO.File]::ReadAllBytes((Join-Path $packagePath 'noyra-package.json'))
                )
                $stagedManifest = Verify-Package -Root $staging -ForceSignature:([bool]$manifest.signature_required)
                if ([string]$stagedManifest.version -cne $version) {
                    throw 'Staged package version changed after initial verification.'
                }
                Move-Item -LiteralPath $staging -Destination $target
            }
            # Only after the candidate has been verified and staged do we
            # persist legacy adoption and begin the transaction.
            if ($null -eq $script:R03CurrentState) {
                $script:R03CurrentState = Get-OrAdopt-R03State
            }
            # The authority transaction is the only commit point. Legacy files
            # are repaired from it and never drive selection after bootstrap.
            $script:R03CurrentState = Invoke-R03InstallTransaction $script:R03CurrentState $version ([bool]$RequireSignature)
        }
        finally {
            if ($null -ne $staging -and (Test-Path -LiteralPath $staging)) {
                $stagingFull = [IO.Path]::GetFullPath($staging)
                if (-not $stagingFull.StartsWith($versions + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Staging cleanup escaped the versions directory.'
                }
                Remove-Item -LiteralPath $stagingFull -Recurse -Force
            }
        }
    }
    'upgrade' {
        if ([string]::IsNullOrWhiteSpace($PackageRoot)) { throw '-PackageRoot is required for upgrade.' }
        & $PSCommandPath -Action install -InstallRoot $InstallRoot -DataRoot $DataRoot -PackageRoot $PackageRoot -RequireSignature:$RequireSignature -TrustedPublisherThumbprints $TrustedPublisherThumbprints
    }
    'rollback' {
        $script:R03CurrentState = Invoke-R03RollbackTransaction $script:R03CurrentState
    }
    'start' {
        $null = Invoke-R03AuthorizeStart $script:R03CurrentState
        Start-Noyra
    }
    'recover' {
        if ($null -eq $script:R03CurrentState) { throw 'No R03 state is available to recover.' }
        $script:R03CurrentState = Complete-R03Recovery $script:R03CurrentState
        [pscustomobject]@{ phase = $script:R03CurrentState.phase; generation = $script:R03CurrentState.generation; current_version = $script:R03CurrentState.current; previous_version = $script:R03CurrentState.previous } | ConvertTo-Json -Compress
    }
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
        $current = if ($null -ne $script:R03CurrentState) { $script:R03CurrentState.current } else { Get-CurrentVersion }
        $previous = if ($null -ne $script:R03CurrentState) { $script:R03CurrentState.previous } else { Read-Pointer $previousPointer }
        [pscustomobject]@{
            install_root = $InstallRoot
            data_root = $DataRoot
            current_version = $current
            previous_version = $previous
            running = $running
            protocol_version = if ($null -ne $script:R03CurrentState -or $null -ne $script:R03TransitionState) { 1 } else { 0 }
            recovery_phase = if ($null -ne $script:R03TransitionState) { 'pre_acceptance_transition' } elseif ($null -ne $script:R03CurrentState) { $script:R03CurrentState.phase } else { 'legacy' }
            recovery_target_version = if ($null -ne $script:R03TransitionState) { $script:R03TransitionState.version } elseif ($null -ne $script:R03CurrentState -and $null -ne $script:R03CurrentState.intent) { $script:R03CurrentState.intent.version } else { $null }
            recovery_kind = if ($null -ne $script:R03TransitionState) { $script:R03TransitionState.kind } elseif ($null -ne $script:R03CurrentState -and $null -ne $script:R03CurrentState.intent) { $script:R03CurrentState.intent.kind } else { $null }
            generation = if ($null -ne $script:R03CurrentState) { $script:R03CurrentState.generation } else { $null }
            recovery_required = ($null -ne $script:R03TransitionState -or ($null -ne $script:R03CurrentState -and $script:R03CurrentState.phase -ne 'ready'))
        } | ConvertTo-Json -Compress
    }
}

}
finally { if ($null -ne $instanceLock) { $instanceLock.Dispose() } }
