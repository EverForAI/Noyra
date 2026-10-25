param([string]$Lifecycle,[string]$WorkRoot)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security.Cryptography.Pkcs
$results=[ordered]@{}
$utf8=[Text.UTF8Encoding]::new($false)
$exe=Join-Path $env:WINDIR 'System32/whoami.exe'
$exeSignature=Get-AuthenticodeSignature -LiteralPath $exe
if ($exeSignature.Status -ne 'Valid') { throw 'Signed Windows fixture missing.' }
$osPin=$exeSignature.SignerCertificate.Thumbprint
$rsaA=[Security.Cryptography.RSA]::Create(2048)
$rsaB=[Security.Cryptography.RSA]::Create(2048)
# Reuse the exact existing fixture builders, not a weaker simulated verifier.
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'windows-verification-regression.ps1'),[ref]$tokens,[ref]$errors)
foreach ($name in @('New-MemoryCertificate','Make-Package')) {
    $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$false)
    if ($null -eq $definition) { throw 'Fixture builder missing.' }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$certA=New-MemoryCertificate $rsaA 'Noyra R03 fixture A'
$certB=New-MemoryCertificate $rsaB 'Noyra R03 fixture B'
$pinsA=@($certA.Thumbprint,$osPin)
$pinsB=@($certB.Thumbprint,$osPin)
$pinsAB=@($certA.Thumbprint,$certB.Thumbprint,$osPin)
try {
    $a1=Make-Package 'a1' $true $certA $pinsA
    $a2=Make-Package 'a2' $true $certA $pinsA
    $a3=Make-Package 'a3' $true $certA $pinsA
    $b3=Make-Package 'b3' $true $certB $pinsB
    $u1=Make-Package 'u1' $false $null @()
    $u2=Make-Package 'u2' $false $null @()
    $strict=Join-Path $WorkRoot 'strict-base'
    $strictData=Join-Path $WorkRoot 'strict-data'
    & $Lifecycle -Action install -InstallRoot $strict -DataRoot $strictData -PackageRoot $a1 -TrustedPublisherThumbprints $pinsA | Out-Null
    & $Lifecycle -Action upgrade -InstallRoot $strict -DataRoot $strictData -PackageRoot $a2 | Out-Null
    foreach ($version in @('a3','b3')) {
        Copy-Item -LiteralPath (Join-Path $WorkRoot $version) -Destination (Join-Path $strict ('versions/' + $version)) -Recurse
    }
    $development=Join-Path $WorkRoot 'development-base'
    $developmentData=Join-Path $WorkRoot 'development-data'
    & $Lifecycle -Action install -InstallRoot $development -DataRoot $developmentData -PackageRoot $u1 | Out-Null
    & $Lifecycle -Action upgrade -InstallRoot $development -DataRoot $developmentData -PackageRoot $u2 | Out-Null
    foreach ($version in @('a3','b3')) {
        Copy-Item -LiteralPath (Join-Path $WorkRoot $version) -Destination (Join-Path $development ('versions/' + $version)) -Recurse
    }
    $metadata=[ordered]@{strict_base=$strict; development_base=$development;
        pin_a=$certA.Thumbprint; pin_b=$certB.Thumbprint; pin_os=$osPin;
        pins_a=@($pinsA); pins_b=@($pinsB); pins_ab=@($pinsAB); package_b=$b3}
    $json=$metadata | ConvertTo-Json -Depth 4 -Compress
    [IO.File]::WriteAllText((Join-Path $WorkRoot 'fixture.json'),$json,$utf8)
    $json
}
finally { $certA.Dispose();$certB.Dispose();$rsaA.Dispose();$rsaB.Dispose() }
