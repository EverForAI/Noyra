param([string]$Lifecycle, [string]$WorkRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security.Cryptography.Pkcs
$results = [ordered]@{}
$utf8 = [Text.UTF8Encoding]::new($false)
$exe = Join-Path $env:WINDIR 'System32/whoami.exe'
$exeSignature = Get-AuthenticodeSignature -LiteralPath $exe
if ($exeSignature.Status -ne 'Valid') { throw 'A valid signed system fixture is required.' }
$osPin = $exeSignature.SignerCertificate.Thumbprint
$rsaA = [Security.Cryptography.RSA]::Create(2048)
$rsaB = [Security.Cryptography.RSA]::Create(2048)
function New-MemoryCertificate($Rsa, [string]$Name) {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
        "CN=$Name", $Rsa, [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    return $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddHours(-1), [DateTimeOffset]::UtcNow.AddDays(1))
}
$certA = New-MemoryCertificate $rsaA 'Noyra local fixture A'
$certB = New-MemoryCertificate $rsaB 'Noyra local fixture B'
$pinsA = @($certA.Thumbprint, $osPin)
$pinsAB = @($certA.Thumbprint, $certB.Thumbprint, $osPin)
$pinsB = @($certB.Thumbprint, $osPin)
function Make-Package([string]$Version, [bool]$Strict, $Certificate, [string[]]$Metadata) {
    $path = Join-Path $WorkRoot $Version
    New-Item -ItemType Directory -Path $path | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $path 'noyra.exe')
    [IO.File]::WriteAllText((Join-Path $path 'module.py'), "original-$Version", $utf8)
    $document = [ordered]@{
        format = 'noyra-windows-package/v1'; version = $Version; launcher = 'noyra.exe'
        signature_required = $Strict; trusted_publisher_thumbprints = @($Metadata)
        files = @('noyra.exe', 'module.py') | ForEach-Object {
            @{path = $_; sha256 = (Get-FileHash -LiteralPath (Join-Path $path $_) -Algorithm SHA256).Hash.ToLowerInvariant()}
        }
    }
    $manifest = Join-Path $path 'noyra-package.json'
    [IO.File]::WriteAllText($manifest, ($document | ConvertTo-Json -Depth 8 -Compress), $utf8)
    if ($Strict) {
        $cms = [Security.Cryptography.Pkcs.SignedCms]::new(
            [Security.Cryptography.Pkcs.ContentInfo]::new([IO.File]::ReadAllBytes($manifest)), $true)
        $cms.ComputeSignature([Security.Cryptography.Pkcs.CmsSigner]::new($Certificate), $true)
        [IO.File]::WriteAllBytes(($manifest + '.p7s'), $cms.Encode())
    }
    return $path
}
function Invoke-Life([string]$Action, [string]$RootName, [string]$Package = '', [hashtable]$Extra = @{}) {
    $arguments = @{
        Action = $Action; InstallRoot = (Join-Path $WorkRoot $RootName)
        DataRoot = (Join-Path $WorkRoot ($RootName + '-data'))
    }
    if ($Package) { $arguments.PackageRoot = $Package }
    foreach ($key in $Extra.Keys) { $arguments[$key] = $Extra[$key] }
    try {
        & $Lifecycle @arguments | Out-Null
        return @{ok = $true; error = ''}
    } catch { return @{ok = $false; error = $_.Exception.Message} }
}
function Expect([string]$Name, $Result, [bool]$Success, [string]$ErrorPattern = '') {
    if ($Result.ok -ne $Success -or ($ErrorPattern -and $Result.error -notmatch $ErrorPattern)) {
        throw "$Name failed: $($Result | ConvertTo-Json -Compress)"
    }
    $results[$Name] = $true
}
function Assert-Current([string]$RootName, [string]$Expected) {
    $value = (Get-Content -LiteralPath (Join-Path $WorkRoot "$RootName/current.txt") -Raw).Trim()
    if ($value -ne $Expected) { throw "Pointer changed unexpectedly: $RootName $value" }
}
function Set-Trust([string]$RootName, [string[]]$Pins) {
    @{format = 'noyra-publisher-trust/v1'; thumbprints = @($Pins)} | ConvertTo-Json -Compress |
        Set-Content -LiteralPath (Join-Path $WorkRoot "$RootName/trusted-publishers.json") -Encoding utf8
}
try {
    $a1 = Make-Package 'signed-a1' $true $certA $pinsA
    $a2 = Make-Package 'signed-a2' $true $certA $pinsA
    $b3 = Make-Package 'signed-b3' $true $certB $pinsB
    $u1 = Make-Package 'unsigned-u1' $false $null @()
    $u2 = Make-Package 'unsigned-u2' $false $null @()
    $u3 = Make-Package 'unsigned-u3' $false $null @()
    # Fixed state paths are files, never directory containers. Reject before
    # staging or changing trust, receipts or either version selection.
    foreach ($stateName in @('current.txt', 'previous.txt', 'trusted-publishers.json')) {
        $rootName = 'state-' + $stateName.Replace('.', '-')
        $root = Join-Path $WorkRoot $rootName
        $blocked = Join-Path $root $stateName
        # All fault injection and nonrecursive cleanup stay inside the pytest fixture.
        $fixturePrefix = [IO.Path]::GetFullPath($WorkRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not [IO.Path]::GetFullPath($blocked).StartsWith($fixturePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'State fixture escaped its temporary workspace.'
        }
        New-Item -ItemType Directory -Path $blocked -Force | Out-Null
        $marker = Join-Path $blocked 'retained.txt'
        [IO.File]::WriteAllText($marker, 'retained', $utf8)
        Expect ($rootName + '_fresh_install_rejected') (Invoke-Life install $rootName $a1 @{TrustedPublisherThumbprints = $pinsA}) $false 'state path must be a regular file'
        if (@(Get-ChildItem -LiteralPath $blocked -Force).Count -ne 1 -or
            [IO.File]::ReadAllText($marker) -cne 'retained' -or
            (Test-Path -LiteralPath (Join-Path $root 'versions'))) {
            throw 'Rejected directory was modified or package staging began.'
        }
        [IO.File]::Delete($marker)
        [IO.Directory]::Delete($blocked)
        Expect ($rootName + '_initial_retry') (Invoke-Life install $rootName $a1 @{TrustedPublisherThumbprints = $pinsA}) $true
        Expect ($rootName + '_base_upgrade') (Invoke-Life upgrade $rootName $a2) $true
        $retained = @{}
        foreach ($name in @('current.txt', 'previous.txt', 'trusted-publishers.json')) {
            $retained[$name] = [IO.File]::ReadAllBytes((Join-Path $root $name))
        }
        [IO.File]::Delete($blocked)
        New-Item -ItemType Directory -Path $blocked | Out-Null
        Expect ($rootName + '_upgrade_rejected') (Invoke-Life upgrade $rootName $b3 @{TrustedPublisherThumbprints = $pinsB}) $false 'state path must be a regular file'
        Expect ($rootName + '_rollback_rejected') (Invoke-Life rollback $rootName) $false 'state path must be a regular file'
        if (@(Get-ChildItem -LiteralPath $blocked -Force).Count -ne 0 -or
            (Test-Path -LiteralPath (Join-Path $root 'versions/signed-b3')) -or
            (Test-Path -LiteralPath (Join-Path $root 'verification-policy.signed-b3.json'))) {
            throw 'Rejected state type changed installation publication state.'
        }
        foreach ($name in $retained.Keys) {
            if ($name -ceq $stateName) { continue }
            if ([Convert]::ToBase64String($retained[$name]) -cne
                [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $root $name)))) {
                throw 'Rejected state type changed another state file.'
            }
        }
        $statusResult = Invoke-Life status $rootName
        Expect ($rootName + '_status_uses_authority') $statusResult $true
        $status = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot $rootName) -DataRoot (Join-Path $WorkRoot ($rootName + '-data')) | ConvertFrom-Json
        if ($status.current_version -cne 'signed-a2' -or $status.protocol_version -ne 1) {
            throw 'Status trusted a compatibility mirror instead of R03 authority.'
        }
        $results[$rootName + '_status_uses_authority'] = $true
        [IO.Directory]::Delete($blocked)
        [IO.File]::WriteAllBytes($blocked, $retained[$stateName])
        Expect ($rootName + '_upgrade_retry') (Invoke-Life upgrade $rootName $b3 @{TrustedPublisherThumbprints = $pinsAB}) $true
        Assert-Current $rootName 'signed-b3'
        Expect ($rootName + '_normal_rollback') (Invoke-Life rollback $rootName) $true
        Assert-Current $rootName 'signed-a2'
    }
    Expect 'explicit_multiple_pins_install' (Invoke-Life install 'strict' $a1 @{TrustedPublisherThumbprints = $pinsA; RequireSignature = $true}) $true
    $installedManifest = [IO.File]::ReadAllBytes((Join-Path $WorkRoot 'strict/versions/signed-a1/noyra-package.json'))
    if ([Convert]::ToBase64String($installedManifest) -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $a1 'noyra-package.json')))) { throw 'Signed bytes changed during staging.' }
    $results.preserved_signed_bytes = $true
    Expect 'omitted_pins_load_saved_policy' (Invoke-Life upgrade 'strict' $a2) $true
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'strict/versions/signed-a2/module.py'), 'disk damage', $utf8)
    Expect 'damaged_current_payload_rollback' (Invoke-Life rollback 'strict') $true
    Assert-Current 'strict' 'signed-a1'
    Expect 'tampered_previous_payload_rejected' (Invoke-Life rollback 'strict') $false 'hash mismatch'
    Assert-Current 'strict' 'signed-a1'
    Expect 'rotation_upgrade_b' (Invoke-Life upgrade 'strict' $b3 @{TrustedPublisherThumbprints = $pinsAB}) $true
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'strict/versions/signed-b3/noyra-package.json'), '{broken', $utf8)
    Expect 'rotation_and_damaged_manifest_rollback' (Invoke-Life rollback 'strict') $true
    Assert-Current 'strict' 'signed-a1'
    Copy-Item -LiteralPath (Join-Path $b3 'noyra-package.json') -Destination (Join-Path $WorkRoot 'strict/versions/signed-b3/noyra-package.json') -Force
    Expect 'empty_pins_load_saved_policy' (Invoke-Life rollback 'strict' '' @{TrustedPublisherThumbprints = @()}) $true
    Assert-Current 'strict' 'signed-b3'
    # Mutating a compatibility projection cannot change authoritative trust.
    Set-Trust 'strict' $pinsB
    $mirrorStatus = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot 'strict') -DataRoot (Join-Path $WorkRoot 'strict-data') | ConvertFrom-Json
    if ($mirrorStatus.protocol_version -ne 1 -or $mirrorStatus.current_version -cne 'signed-b3') { throw 'Status trusted a modified trust mirror.' }
    Set-Trust 'strict' $pinsAB
    # Actual signer revocation is applied by a transaction and must reject A rollback.
    Expect 'revoked_base_install' (Invoke-Life install 'revoked' $a1 @{TrustedPublisherThumbprints = $pinsA}) $true
    Expect 'revoked_signer_upgrade' (Invoke-Life upgrade 'revoked' $b3 @{TrustedPublisherThumbprints = $pinsB}) $true
    Expect 'revoked_actual_signer_rejected' (Invoke-Life rollback 'revoked') $false 'no publishers authorized|not a trusted publisher'
    Assert-Current 'revoked' 'signed-b3'
    # Rewriting manifest JSON cannot retain a valid detached signature.
    $oldManifest = Join-Path $WorkRoot 'strict/versions/signed-a1/noyra-package.json'
    $validBytes = [IO.File]::ReadAllBytes($oldManifest)
    [IO.File]::WriteAllText($oldManifest, ([Text.Encoding]::UTF8.GetString($validBytes) + ' '), $utf8)
    Expect 'tampered_manifest_rejected' (Invoke-Life rollback 'strict') $false 'signature is invalid'
    Assert-Current 'strict' 'signed-b3'
    [IO.File]::WriteAllBytes($oldManifest, $validBytes)
    $policy = Join-Path $WorkRoot 'strict/trusted-publishers.json'
    [IO.File]::WriteAllText($policy, '{invalid', $utf8)
    $malformedMirror = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot 'strict') -DataRoot (Join-Path $WorkRoot 'strict-data') | ConvertFrom-Json
    if ($malformedMirror.current_version -cne 'signed-b3') { throw 'Malformed trust projection changed authority status.' }
    Set-Trust 'strict' $pinsAB
    $receipt = Join-Path $WorkRoot 'strict/verification-policy.signed-b3.json'
    $validReceipt = [IO.File]::ReadAllBytes($receipt)
    [IO.File]::WriteAllText($receipt, '{"format":"noyra-installed-verification/v1","version":"signed-b3","signature_required":"false"}', $utf8)
    $malformedReceipt = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot 'strict') -DataRoot (Join-Path $WorkRoot 'strict-data') | ConvertFrom-Json
    if ($malformedReceipt.current_version -cne 'signed-b3') { throw 'Malformed receipt projection changed authority status.' }
    $results.malformed_receipt_does_not_override_authority = $true
    [IO.File]::WriteAllBytes($receipt, $validReceipt)
    Expect 'null_pins_load_saved_policy' (Invoke-Life rollback 'strict' '' @{TrustedPublisherThumbprints = $null}) $true
    Remove-Item -LiteralPath (Join-Path $WorkRoot 'strict/verification-policy.signed-a1.json') -Force
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'strict/versions/signed-a1/noyra-package.json'), '{broken', $utf8)
    Expect 'legacy_strict_damaged_current_rollback' (Invoke-Life rollback 'strict') $true
    Assert-Current 'strict' 'signed-b3'
    # Unused metadata is descriptive; revoked actual signers remain rejected above.
    $b4 = Make-Package 'signed-b4' $true $certB $pinsAB
    Expect 'revoked_unused_metadata_does_not_block_b' (Invoke-Life install 'unused-metadata' $b4 @{TrustedPublisherThumbprints = $pinsB}) $true
    Expect 'absent_policy_cannot_trust_package_metadata' (Invoke-Life install 'no-policy' $a1) $false 'external trusted publisher'
    # A single pin reaches CMS verification, then fails on the independent EXE signer.
    $single = Make-Package 'single-cms-pin' $true $certA @($certA.Thumbprint)
    Expect 'single_pin_keeps_executable_verification' (Invoke-Life install 'single' $single @{TrustedPublisherThumbprints = @($certA.Thumbprint)}) $false 'Authenticode'
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot 'saved-single') | Out-Null
    Set-Trust 'saved-single' @($certA.Thumbprint)
    Expect 'saved_single_pin_reaches_executable_verification' (Invoke-Life install 'saved-single' $single) $false 'Authenticode'
    Expect 'package_metadata_cannot_self_authorize' (Invoke-Life install 'untrusted' $a1 @{TrustedPublisherThumbprints = $pinsB}) $false 'not a trusted publisher'
    Expect 'invalid_pin_rejected' (Invoke-Life install 'bad-pin' $a1 @{TrustedPublisherThumbprints = @('not-a-pin')}) $false '40 hexadecimal'
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot 'receipt-dir/verification-policy.signed-b3.json') -Force | Out-Null
    Set-Trust 'receipt-dir' $pinsA
    $retainedTrust = [IO.File]::ReadAllBytes((Join-Path $WorkRoot 'receipt-dir/trusted-publishers.json'))
    Expect 'directory_receipt_cannot_publish_pointer' (Invoke-Life install 'receipt-dir' $b3 @{TrustedPublisherThumbprints = $pinsB}) $false 'verification policy is unsafe'
    if ([Convert]::ToBase64String($retainedTrust) -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $WorkRoot 'receipt-dir/trusted-publishers.json')))) {
        throw 'Failed receipt changed existing external trust.'
    }
    $results.failed_receipt_preserves_existing_trust = $true
    # Repair the external fault and retry the same fully verified version.
    Remove-Item -LiteralPath (Join-Path $WorkRoot 'receipt-dir/verification-policy.signed-b3.json')
    Expect 'receipt_failure_same_version_retry' (Invoke-Life install 'receipt-dir' $b3 @{TrustedPublisherThumbprints = $pinsB}) $true
    Assert-Current 'receipt-dir' 'signed-b3'
    Expect 'selected_version_retry_is_idempotent' (Invoke-Life install 'receipt-dir' $b3) $true
    if (Test-Path -LiteralPath (Join-Path $WorkRoot 'receipt-dir/previous.txt')) { throw 'Idempotent retry invented a rollback pointer.' }
    # A receipt failure with a previously selected version preserves its route.
    Expect 'resume_existing_base' (Invoke-Life install 'resume-existing' $a1 @{TrustedPublisherThumbprints = $pinsA}) $true
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot 'resume-existing/verification-policy.signed-a2.json') | Out-Null
    Expect 'resume_existing_receipt_failure' (Invoke-Life upgrade 'resume-existing' $a2) $false 'verification policy is unsafe'
    $pendingStatus = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot 'resume-existing') -DataRoot (Join-Path $WorkRoot 'resume-existing-data') | ConvertFrom-Json
    if (-not $pendingStatus.recovery_required -or $pendingStatus.recovery_target_version -cne 'signed-a2') { throw 'Accepted receipt failure did not expose its durable intent.' }
    Remove-Item -LiteralPath (Join-Path $WorkRoot 'resume-existing/verification-policy.signed-a2.json')
    & $Lifecycle -Action recover -InstallRoot (Join-Path $WorkRoot 'resume-existing') -DataRoot (Join-Path $WorkRoot 'resume-existing-data') | Out-Null
    Expect 'resume_existing_after_repair' (Invoke-Life upgrade 'resume-existing' $a2) $true
    Expect 'resume_existing_repeat' (Invoke-Life upgrade 'resume-existing' $a2) $true
    Assert-Current 'resume-existing' 'signed-a2'
    if ((Get-Content -LiteralPath (Join-Path $WorkRoot 'resume-existing/previous.txt') -Raw).Trim() -cne 'signed-a1') { throw 'Repeated upgrade destroyed rollback history.' }
    foreach ($phase in @('trust', 'pointer')) {
        $rootName = 'resume-' + $phase
        Expect ($phase + '_base') (Invoke-Life install $rootName $a1 @{TrustedPublisherThumbprints = $pinsA}) $true
        $blockedName = if ($phase -eq 'trust') { 'trusted-publishers.json' } else { 'current.txt' }
        $heldFile = [IO.File]::Open((Join-Path $WorkRoot ($rootName + '/' + $blockedName)),
            [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
        try {
            Expect ($phase + '_failure_is_reported') (Invoke-Life upgrade $rootName $a2 @{TrustedPublisherThumbprints = $pinsA}) $false
        }
        finally { $heldFile.Dispose() }
        $failureStatus = & $Lifecycle -Action status -InstallRoot (Join-Path $WorkRoot $rootName) -DataRoot (Join-Path $WorkRoot ($rootName + '-data')) | ConvertFrom-Json
        if ($failureStatus.recovery_required) { & $Lifecycle -Action recover -InstallRoot (Join-Path $WorkRoot $rootName) -DataRoot (Join-Path $WorkRoot ($rootName + '-data')) | Out-Null }
        Assert-Current $rootName 'signed-a1'
        Expect ($phase + '_failure_same_version_retry') (Invoke-Life upgrade $rootName $a2) $true
        Assert-Current $rootName 'signed-a2'
        if ((Get-Content -LiteralPath (Join-Path $WorkRoot ($rootName + '/previous.txt')) -Raw).Trim() -cne 'signed-a1') { throw 'Recovery lost the prior selected version.' }
    }
    # Neither corrupt nor different same-version contents may be overwritten.
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot 'resume-damaged/versions') -Force | Out-Null
    Copy-Item -LiteralPath $b3 -Destination (Join-Path $WorkRoot 'resume-damaged/versions/signed-b3') -Recurse
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'resume-damaged/versions/signed-b3/module.py'), 'corrupt', $utf8)
    Expect 'retry_reverifies_existing_payload' (Invoke-Life install 'resume-damaged' $b3 @{TrustedPublisherThumbprints = $pinsB}) $false 'hash mismatch'
    Expect 'resume_conflict_base' (Invoke-Life install 'resume-conflict' $u1) $true
    $alternate = Join-Path $WorkRoot 'alternate'
    Copy-Item -LiteralPath $u1 -Destination $alternate -Recurse
    [IO.File]::WriteAllText((Join-Path $alternate 'module.py'), 'different valid payload', $utf8)
    $alternateManifest = Get-Content -LiteralPath (Join-Path $alternate 'noyra-package.json') -Raw | ConvertFrom-Json
    foreach ($entry in $alternateManifest.files) {
        $entry.sha256 = (Get-FileHash -LiteralPath (Join-Path $alternate $entry.path) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    [IO.File]::WriteAllText((Join-Path $alternate 'noyra-package.json'), ($alternateManifest | ConvertTo-Json -Depth 8 -Compress), $utf8)
    Expect 'different_same_version_is_not_replaced' (Invoke-Life install 'resume-conflict' $alternate) $false 'differs from the supplied package'
    Assert-Current 'resume-conflict' 'unsigned-u1'
    if ([IO.File]::ReadAllText((Join-Path $WorkRoot 'resume-conflict/versions/unsigned-u1/module.py')) -cne 'original-unsigned-u1') { throw 'Existing version contents changed.' }
    Expect 'unsigned_initial_install' (Invoke-Life install 'guard' $u1) $true
    Expect 'strict_upgrade_for_recovery_guard' (Invoke-Life upgrade 'guard' $a1 @{TrustedPublisherThumbprints = $pinsA}) $true
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'guard/versions/signed-a1/noyra-package.json'), '{broken', $utf8)
    Expect 'strict_receipt_rejects_unsigned_previous' (Invoke-Life rollback 'guard') $false 'signed manifest must require'
    Assert-Current 'guard' 'signed-a1'
    # Simulate a pre-receipt strict installation: saved trust remains authoritative.
    Remove-Item -LiteralPath (Join-Path $WorkRoot 'guard/verification-policy.signed-a1.json') -Force
    Expect 'legacy_strict_policy_rejects_unsigned_previous' (Invoke-Life rollback 'guard') $false 'signed manifest must require'
    Assert-Current 'guard' 'signed-a1'
    # Preserve the existing per-call policy for deliberate development packages.
    Expect 'development_upgrade_preserves_per_call_policy' (Invoke-Life upgrade 'guard' $u2) $true
    Expect 'development_second_upgrade' (Invoke-Life upgrade 'guard' $u3) $true
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'guard/versions/unsigned-u3/module.py'), 'damage', $utf8)
    Expect 'unsigned_receipt_recovery_with_historical_pins' (Invoke-Life rollback 'guard') $true
    Assert-Current 'guard' 'unsigned-u2'
    Expect 'legacy_unsigned_install' (Invoke-Life install 'legacy' $u1) $true
    Expect 'legacy_unsigned_upgrade' (Invoke-Life upgrade 'legacy' $u2) $true
    Remove-Item -LiteralPath (Join-Path $WorkRoot 'legacy/verification-policy.unsigned-u2.json') -Force
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'legacy/versions/unsigned-u2/module.py'), 'damage', $utf8)
    Expect 'legacy_unsigned_damaged_current_rollback' (Invoke-Life rollback 'legacy') $true
    Assert-Current 'legacy' 'unsigned-u1'
    $results | ConvertTo-Json -Compress
}
finally {
    $certA.Dispose(); $certB.Dispose(); $rsaA.Dispose(); $rsaB.Dispose()
}
