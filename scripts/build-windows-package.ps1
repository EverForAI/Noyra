[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Version,
    [Parameter(Mandatory = $true)] [string]$OutputRoot,
    [Parameter(Mandatory = $false)] [string]$SourceRoot = (Join-Path $PSScriptRoot '..'),
    [Parameter(Mandatory = $false)] [string]$LauncherPath,
    [Parameter(Mandatory = $false)] [string[]]$TrustedPublisherThumbprints,
    [Parameter(Mandatory = $false)] [string]$ManifestSignerThumbprint,
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
$trustedThumbprints = @($TrustedPublisherThumbprints | ForEach-Object {
    $normalized = ([string]$_).Trim().Replace(' ', '').ToUpperInvariant()
    if ($normalized -notmatch '^[0-9A-F]{40}$') { throw 'Trusted publisher thumbprints must be 40 hexadecimal characters.' }
    $normalized
} | Sort-Object -Unique)
if ($RequireSignature -and $trustedThumbprints.Count -eq 0) {
    throw 'RequireSignature requires at least one trusted publisher thumbprint.'
}
if ($RequireSignature -and [string]::IsNullOrWhiteSpace($ManifestSignerThumbprint)) {
    throw 'RequireSignature requires ManifestSignerThumbprint for the detached package manifest signature.'
}
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
        'deploy\windows\noyra-r03-recovery.ps1',
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
    if ([string]::IsNullOrWhiteSpace($LauncherPath)) {
        throw 'LauncherPath is required: provide the signed Noyra launcher executable.'
    }
    $launcher = (Resolve-Path -LiteralPath $LauncherPath).Path
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'LauncherPath must be a regular file.' }
    if ((Get-Item -LiteralPath $launcher).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'LauncherPath must not be a reparse point.'
    }
    Copy-Item -LiteralPath $launcher -Destination (Join-Path $staging 'noyra.exe') -Force
    $launcherRelative = 'noyra.exe'
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
            $signature = Get-AuthenticodeSignature -LiteralPath $executable.FullName
            if ($signature.Status -ne 'Valid' -or
                $trustedThumbprints -notcontains $signature.SignerCertificate.Thumbprint.ToUpperInvariant()) {
                throw "Executable is not Authenticode-signed: $($executable.FullName)"
            }
        }
    }
    $manifest = [ordered]@{
        format = 'noyra-windows-package/v1'
        version = $Version
        created_at = [DateTime]::UtcNow.ToString('o')
        launcher = $launcherRelative
        trusted_publisher_thumbprints = @($trustedThumbprints)
        files = @($entries | Sort-Object path)
        signature_required = [bool]$RequireSignature
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 6 -Compress
    $manifestPath = Join-Path $staging 'noyra-package.json'
    [IO.File]::WriteAllText($manifestPath, $manifestJson, [Text.UTF8Encoding]::new($false))
    if ($RequireSignature) {
        try {
            Add-Type -AssemblyName System.Security.Cryptography.Pkcs -ErrorAction Stop
            $normalizedSigner = ([string]$ManifestSignerThumbprint).Trim().Replace(' ', '').ToUpperInvariant()
            if ($normalizedSigner -notmatch '^[0-9A-F]{40}$' -or $trustedThumbprints -notcontains $normalizedSigner) {
                throw 'ManifestSignerThumbprint must be one of the trusted publisher thumbprints.'
            }
            $certificate = Get-ChildItem Cert:\CurrentUser\My |
                Where-Object { $_.Thumbprint -eq $normalizedSigner } |
                Select-Object -First 1
            if ($null -eq $certificate -or -not $certificate.HasPrivateKey) {
                throw 'The manifest signer certificate with a private key is unavailable.'
            }
            $content = [System.Security.Cryptography.Pkcs.ContentInfo]::new([IO.File]::ReadAllBytes($manifestPath))
            $cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($content, $true)
            $signer = [System.Security.Cryptography.Pkcs.CmsSigner]::new($certificate)
            $signer.IncludeOption = [System.Security.Cryptography.X509Certificates.X509IncludeOption]::EndCertOnly
            $cms.ComputeSignature($signer)
            [IO.File]::WriteAllBytes((Join-Path $staging 'noyra-package.json.p7s'), $cms.Encode())
        }
        catch {
            throw ('Unable to create detached package manifest signature: ' + $_.Exception.Message)
        }
    }
    Move-Item -LiteralPath $staging -Destination $destination
    Write-Output $destination
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
