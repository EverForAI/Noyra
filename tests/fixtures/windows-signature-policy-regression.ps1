param([string]$Lifecycle, [string]$WorkRoot, [string]$FixtureJson)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'windows-r03-protocol-prototype.ps1')
Import-R03LifecycleHelpers $Lifecycle
$fixture=Get-Content -LiteralPath $FixtureJson -Raw | ConvertFrom-Json
$packageRoot=Split-Path -Parent $FixtureJson
$pinsA=@($fixture.pins_a);$pinsB=@($fixture.pins_b);$pinsAB=@($fixture.pins_ab)
$productionCases=[ordered]@{}
$prototypeCases=[ordered]@{}

function Assert-Policy([bool]$Value,[string]$Reason) { if (-not $Value) { throw $Reason } }
function New-PolicyCase([string]$Name,[bool]$Development) {
    $root=Join-Path $WorkRoot $Name
    $base=if($Development){$fixture.development_base}else{$fixture.strict_base}
    Copy-Item -LiteralPath $base -Destination $root -Recurse
    $data=$root+'-data'
    New-Item -ItemType Directory -Path $data | Out-Null
    [IO.File]::WriteAllText((Join-Path $data 'sentinel'),'retained-subject-fixture')
    return @{root=$root;data=$data}
}
function Read-ProductAuthority([hashtable]$Case) {
    $envelope=Get-Content -LiteralPath (Join-Path $Case.root 'r03-state.json') -Raw | ConvertFrom-Json
    Assert-Policy ($envelope.format -ceq 'noyra-r03-envelope/v1') 'Unexpected product envelope.'
    $bytes=[Convert]::FromBase64String($envelope.payload)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { $digest=([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
    Assert-Policy ($digest -ceq $envelope.sha256) 'Product authority hash mismatch.'
    $state=[Text.UTF8Encoding]::new($false,$true).GetString($bytes) | ConvertFrom-Json
    Assert-Policy ($state.format -ceq 'noyra-r03-state/v1') 'Unexpected product state.'
    return $state
}
function Policy-Projection($State) {
    return [pscustomobject][ordered]@{
        current=$State.current;previous=$State.previous;strict=[bool]$State.strict
        pins=@($State.pins | Sort-Object);generation=[long]$State.generation;phase=$State.phase
    }
}
function Assert-PolicyState($State,[string]$Version,[bool]$Strict,[string[]]$Pins) {
    Assert-Policy ($State.phase -ceq 'ready' -and $State.current -ceq $Version -and
        [bool]$State.strict -eq $Strict -and
        (@($State.pins | Sort-Object) -join ',') -ceq (@($Pins | Sort-Object) -join ',')) 'Unexpected selected signature policy.'
}
function Assert-DataSentinel([hashtable]$Case) {
    Assert-Policy (([IO.File]::ReadAllText((Join-Path $Case.data 'sentinel'))) -ceq 'retained-subject-fixture') 'DataRoot sentinel changed.'
}
function Expected-Pins([string]$Name) {
    switch ($Name) { 'A' {return @($pinsA)} 'B' {return @($pinsB)} 'AB' {return @($pinsAB)} default {return @()} }
}

# These cases execute the real lifecycle entry. The protocol model is checked
# separately against the same explicit policy inputs, not used as the oracle.
$cases=@(
 @{name='strict_to_development_saved_pins';dev=$false;version='u1';pins='saved';require=$false;accept=$true;strict=$false;expectedPins='A'},
 @{name='development_cannot_replace_saved_trust';dev=$false;version='u1';pins='B';require=$false;accept=$true;strict=$false;expectedPins='A'},
 @{name='explicit_strict_rejects_development';dev=$false;version='u1';pins='saved';require=$true;accept=$false;strict=$true;expectedPins='A'},
 @{name='signed_manifest_keeps_strict';dev=$false;version='a3';pins='A';require=$false;accept=$true;strict=$true;expectedPins='A'},
 @{name='signed_inherits_saved_pins';dev=$false;version='a3';pins='saved';require=$false;accept=$true;strict=$true;expectedPins='A'},
 @{name='rotation_revokes_A';dev=$false;version='b3';pins='B';require=$false;accept=$true;strict=$true;expectedPins='B'},
 @{name='rotation_retains_A';dev=$false;version='b3';pins='AB';require=$false;accept=$true;strict=$true;expectedPins='AB'},
 @{name='untrusted_manifest_signer_rejected';dev=$false;version='b3';pins='saved';require=$false;accept=$false;strict=$true;expectedPins='A'},
 @{name='revoked_actual_signer_rejected';dev=$false;version='a3';pins='B';require=$false;accept=$false;strict=$true;expectedPins='A'},
 @{name='development_to_development';dev=$true;version='u1';pins='saved';require=$false;accept=$true;strict=$false;expectedPins='none'},
 @{name='explicit_strict_rejects_development_base';dev=$true;version='u1';pins='saved';require=$true;accept=$false;strict=$false;expectedPins='none'},
 @{name='development_to_signed';dev=$true;version='a3';pins='A';require=$false;accept=$true;strict=$true;expectedPins='A'},
 @{name='signed_metadata_cannot_grant_trust';dev=$true;version='a3';pins='saved';require=$false;accept=$false;strict=$false;expectedPins='none'},
 @{name='same_version_policy_change_rejected';dev=$false;version='a2';pins='AB';require=$false;accept=$false;strict=$true;expectedPins='A'},
 @{name='same_version_retry_is_idempotent';dev=$false;version='a2';pins='saved';require=$false;accept=$true;strict=$true;expectedPins='A'}
)
foreach ($scenario in $cases) {
    $product=New-PolicyCase ('product-'+$scenario.name) $scenario.dev
    & $Lifecycle -Action recover -InstallRoot $product.root -DataRoot $product.data | Out-Null
    $before=Read-ProductAuthority $product
    $productBefore=Policy-Projection $before
    $parameters=@{Action='upgrade';InstallRoot=$product.root;DataRoot=$product.data
        PackageRoot=(Join-Path $packageRoot $scenario.version);RequireSignature=$scenario.require}
    if($scenario.pins -cne 'saved'){$parameters.TrustedPublisherThumbprints=@(Expected-Pins $scenario.pins)}
    $accepted=$true;$failure=$null
    try { & $Lifecycle @parameters | Out-Null }
    catch {$accepted=$false;$failure=$_.Exception.Message}
    Assert-Policy ($accepted -eq $scenario.accept) ("Product policy mismatch for "+$scenario.name+": "+$failure)
    $after=Read-ProductAuthority $product
    $version=if($accepted){$scenario.version}else{$before.current}
    Assert-PolicyState $after $version $scenario.strict @(Expected-Pins $scenario.expectedPins)
    if(-not $accepted -or $scenario.version -ceq $before.current){
        Assert-Policy (((Policy-Projection $after)|ConvertTo-Json -Compress) -ceq
            ($productBefore|ConvertTo-Json -Compress)) 'Rejected or idempotent product operation changed policy.'
    }
    else {Assert-Policy ($after.generation -eq $before.generation+1) 'Product accepted the wrong generation.'}
    if($accepted){
        $receipt=Get-Content -LiteralPath (Join-Path $product.root ('verification-policy.'+$version+'.json')) -Raw | ConvertFrom-Json
        Assert-Policy ([bool]$receipt.signature_required -eq [bool]$after.strict) 'Product receipt and authority disagree.'
    }
    Assert-DataSentinel $product
    $productionCases[$scenario.name]=[ordered]@{accepted=$accepted;state=(Policy-Projection $after)}

    $model=New-PolicyCase ('prototype-'+$scenario.name) $scenario.dev
    Initialize-R03Probe $model.root $model.data
    $initialPins=if($scenario.dev){@()}else{@($pinsA)}
    Invoke-R03Probe -Action adopt -Pins $initialPins -Strict:(-not $scenario.dev) | Out-Null
    $candidate=Join-Path $model.root ('versions/'+$scenario.version)
    if(-not [IO.Directory]::Exists($candidate)){
        Copy-Item -LiteralPath (Join-Path $packageRoot $scenario.version) -Destination $candidate -Recurse
    }
    $modelAccepted=$true;$modelFailure=$null
    try {Invoke-R03Probe -Action upgrade -Version $scenario.version -Pins @(Expected-Pins $scenario.pins) -Strict:$scenario.require | Out-Null}
    catch {$modelAccepted=$false;$modelFailure=$_.Exception.Message}
    Assert-Policy ($modelAccepted -eq $accepted) ("Production/prototype policy mismatch for "+$scenario.name+": "+$modelFailure)
    $modelState=Invoke-R03Probe -Action status
    $projection=Policy-Projection $modelState
    Assert-Policy (($projection|ConvertTo-Json -Compress) -ceq
        ((Policy-Projection $after)|ConvertTo-Json -Compress)) ("Production/prototype state mismatch: "+$scenario.name)
    Assert-DataSentinel $model
    $prototypeCases[$scenario.name]=[ordered]@{accepted=$modelAccepted;state=$projection}
}
[ordered]@{
 format='noyra-windows-signature-policy/v1'
 production=[ordered]@{implementation='production-lifecycle';cases=$productionCases}
 prototype=[ordered]@{implementation='test-only-protocol';cases=$prototypeCases}
 safety=[ordered]@{launchers_executed=0;certificates_imported=0}
} | ConvertTo-Json -Depth 12 -Compress
