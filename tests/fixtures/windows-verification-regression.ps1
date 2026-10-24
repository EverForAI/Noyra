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
    Set-Trust 'strict' $pinsB
    Expect 'revoked_actual_signer_rejected' (Invoke-Life rollback 'strict') $false 'not a trusted publisher'
    Assert-Current 'strict' 'signed-b3'
    Set-Trust 'strict' $pinsAB
    # Rewriting manifest JSON cannot retain a valid detached signature.
    $oldManifest = Join-Path $WorkRoot 'strict/versions/signed-a1/noyra-package.json'
    $validBytes = [IO.File]::ReadAllBytes($oldManifest)
    [IO.File]::WriteAllText($oldManifest, ([Text.Encoding]::UTF8.GetString($validBytes) + ' '), $utf8)
    Expect 'tampered_manifest_rejected' (Invoke-Life rollback 'strict') $false 'signature is invalid'
    Assert-Current 'strict' 'signed-b3'
    [IO.File]::WriteAllBytes($oldManifest, $validBytes)
    $policy = Join-Path $WorkRoot 'strict/trusted-publishers.json'
    [IO.File]::WriteAllText($policy, '{invalid', $utf8)
    Expect 'malformed_saved_policy_rejected' (Invoke-Life rollback 'strict') $false
    Assert-Current 'strict' 'signed-b3'
    Set-Trust 'strict' $pinsAB
    $receipt = Join-Path $WorkRoot 'strict/verification-policy.signed-b3.json'
    $validReceipt = [IO.File]::ReadAllBytes($receipt)
    [IO.File]::WriteAllText($receipt, '{"format":"noyra-installed-verification/v1","version":"signed-b3","signature_required":"false"}', $utf8)
    Expect 'malformed_recovery_requirement_rejected' (Invoke-Life rollback 'strict') $false 'verification policy is invalid'
    Assert-Current 'strict' 'signed-b3'
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
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot 'receipt-dir/verification-policy.signed-a1.json') -Force | Out-Null
    Expect 'directory_receipt_cannot_publish_pointer' (Invoke-Life install 'receipt-dir' $a1 @{TrustedPublisherThumbprints = $pinsA}) $false 'verification policy is unsafe'
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
