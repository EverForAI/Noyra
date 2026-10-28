# Read-only host capability probe. This is not a signed-protocol acceptance test.
$ErrorActionPreference='Stop'
$cms=$false; $frameworkCms=$false
try { Add-Type -AssemblyName System.Security.Cryptography.Pkcs -ErrorAction Stop; $cms=$true } catch {}
if (-not $cms) {
    try { Add-Type -AssemblyName System.Security -ErrorAction Stop; $frameworkCms=($null -ne ('System.Security.Cryptography.Pkcs.SignedCms' -as [type])) } catch {}
}
[ordered]@{major=$PSVersionTable.PSVersion.Major; version=$PSVersionTable.PSVersion.ToString();
    exact_product_assembly_available=$cms; framework_cms_type_available=$frameworkCms;
    declared_signed_release_runtime='PowerShell 7'} | ConvertTo-Json -Compress
