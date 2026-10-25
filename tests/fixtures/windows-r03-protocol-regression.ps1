param([string]$Lifecycle,[string]$WorkRoot,[string]$FixtureJson,[string]$PowerShell)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'windows-r03-protocol-prototype.ps1')
Import-R03LifecycleHelpers $Lifecycle
$fixture=Get-Content -LiteralPath $FixtureJson -Raw | ConvertFrom-Json
$pinsA=@($fixture.pins_a); $pinsB=@($fixture.pins_b); $pinsAB=@($fixture.pins_ab)
$results=[ordered]@{}
$worker=Join-Path $PSScriptRoot 'windows-r03-protocol-worker.ps1'
function Assert-That([bool]$Condition,[string]$Reason) { if (-not $Condition) { throw $Reason } }
function Must-Fail([scriptblock]$Operation,[string]$Pattern='') {
    $failed=$false
    try { & $Operation | Out-Null }
    catch {
        $failed=$true
        if ($Pattern -and $_.Exception.Message -notmatch $Pattern) { throw }
    }
    if (-not $failed) { throw ("Expected explicit failure at test line $($MyInvocation.ScriptLineNumber), got success.") }
}
function New-Case([string]$Name,[switch]$Development) {
    Assert-R03Segment $Name
    $root=Join-Path $WorkRoot $Name
    $source=if ($Development) { $fixture.development_base } else { $fixture.strict_base }
    Copy-Item -LiteralPath $source -Destination $root -Recurse
    $data=$root+'-data'
    New-Item -ItemType Directory -Path $data | Out-Null
    [IO.File]::WriteAllText((Join-Path $data 'subject-sentinel'),'retained-data')
    Initialize-R03Probe $root $data
    return @{root=$root;data=$data}
}
function Adopt-Case([string]$Name) {
    $case=New-Case $Name
    Invoke-R03Probe -Action adopt -Pins $pinsA | Out-Null
    return $case
}
function Child([hashtable]$Case,[string]$Action,[string]$Version='',[string[]]$Pins=@(),[string]$Fault='',
    [bool]$Crash=$false,[string]$Transaction='txn-1',[long]$Generation=0) {
    $arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$worker,
        '-Lifecycle',$Lifecycle,'-Root',$Case.root,'-Data',$Case.data,'-Action',$Action,
        '-Transaction',$Transaction,'-BaseGeneration',[string]$Generation)
    if ($Version) { $arguments+=@('-Version',$Version) }
    if (@($Pins).Count) { $arguments+=@('-Pins',($Pins -join ',')) }
    if ($Fault) { $arguments+=@('-Fault',$Fault) }
    if ($Crash) { $arguments+='-Crash' }
    $start=[Diagnostics.ProcessStartInfo]::new()
    $start.FileName=$PowerShell
    # Arguments are controlled paths/segments, passed directly to CreateProcess.
    $start.Arguments=(@($arguments | ForEach-Object { '"'+$_.Replace('"','\"')+'"' }) -join ' ')
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $start.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $process=[Diagnostics.Process]::new();$process.StartInfo=$start
    try {
        $null=$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill();$null=$process.WaitForExit(5000);throw 'Prototype worker exceeded test watchdog.'
        }
        return @{code=$process.ExitCode;output=$stdout.GetAwaiter().GetResult();error=$stderr.GetAwaiter().GetResult()}
    }
    finally { $process.Dispose() }
}
function Assert-Data([hashtable]$Case) {
    Assert-That ([IO.File]::ReadAllText((Join-Path $Case.data 'subject-sentinel')) -ceq 'retained-data') 'DataRoot changed.'
    foreach ($version in @('a1','a2','a3','b3')) {
        Assert-That ([IO.Directory]::Exists((Join-Path $Case.root ('versions/'+$version)))) 'Referenced package disappeared.'
    }
}
function Restore-ProbeContext([hashtable]$Case) { Initialize-R03Probe $Case.root $Case.data }

# Production regression: a locked current mirror leaves COMMITTED authority,
# then explicit recovery repairs projections without resurrecting signer A.
$case=New-Case 'product-transaction'
& $Lifecycle -Action recover -InstallRoot $case.root -DataRoot $case.data | Out-Null
[IO.File]::WriteAllText((Join-Path $case.root 'verification-policy.b3.json'),'')
$held=[IO.File]::Open((Join-Path $case.root 'verification-policy.b3.json'),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {
    Must-Fail { & $Lifecycle -Action upgrade -InstallRoot $case.root -DataRoot $case.data -PackageRoot $fixture.package_b -TrustedPublisherThumbprints $pinsB }
}
finally { $held.Dispose() }
$productStatus=& $Lifecycle -Action status -InstallRoot $case.root -DataRoot $case.data | ConvertFrom-Json
Assert-That ($productStatus.protocol_version -eq 1 -and $productStatus.recovery_phase -cin @('prepared','committed') -and $productStatus.recovery_required -and $productStatus.recovery_target_version -ceq 'b3') ('Status did not expose accepted recovery state: '+($productStatus|ConvertTo-Json -Compress))
Assert-That ($productStatus.current_version -cin @('a2','b3')) 'Status selected an invalid authority version.'
Assert-That ($productStatus.current_version -cin @('a2','b3')) 'Status selected a version outside the pending/committed authority.'
Must-Fail { Read-Pointer (Join-Path $case.root 'current.txt') }
Must-Fail { Read-Pointer (Join-Path $case.root 'previous.txt') }
Must-Fail { Read-PublisherTrustPolicy } 'unsupported format'
& $Lifecycle -Action recover -InstallRoot $case.root -DataRoot $case.data | Out-Null
Assert-That ((Read-Pointer (Join-Path $case.root 'current.txt')) -ceq 'b3') 'Recovery failed to repair the current mirror.'
Assert-That ((Read-Pointer (Join-Path $case.root 'previous.txt')) -ceq 'a2') 'Recovery lost previous selection.'
Assert-That (@(Read-PublisherTrustPolicy) -notcontains $fixture.pin_a) 'Recovery resurrected revoked signer A.'
$productStatus=& $Lifecycle -Action status -InstallRoot $case.root -DataRoot $case.data | ConvertFrom-Json
Assert-That ($productStatus.recovery_phase -ceq 'ready' -and -not $productStatus.recovery_required) 'Recovery did not reach ready.'
Must-Fail { & $Lifecycle -Action rollback -InstallRoot $case.root -DataRoot $case.data } 'not trusted publisher|no publishers authorized|not a trusted publisher'
Assert-That ((Read-Pointer (Join-Path $case.root 'current.txt')) -ceq 'b3') 'Rejected revoked rollback changed current.'
Assert-Data $case
$results.production_r03_transaction_recovers=$true
$results.production_r03_status_uses_authority=$true
$results.production_r03_revocation_survives_recovery=$true

# Legacy adoption, authority-first status, marker repair, and an actual rollback
# all go through the production lifecycle without executing a package launcher.
$case=New-Case 'product-rollback'
$packageRoot=Split-Path -Parent $FixtureJson
& $Lifecycle -Action install -InstallRoot $case.root -DataRoot $case.data -PackageRoot (Join-Path $packageRoot 'a2') -TrustedPublisherThumbprints $pinsA | Out-Null
Write-PointerAtomic (Join-Path $case.root 'current.txt') 'a1'
$staleStatus=& $Lifecycle -Action status -InstallRoot $case.root -DataRoot $case.data | ConvertFrom-Json
Assert-That ($staleStatus.current_version -ceq 'a2' -and $staleStatus.previous_version -ceq 'a1') 'Status trusted stale pointer mirrors.'
& $Lifecycle -Action rollback -InstallRoot $case.root -DataRoot $case.data | Out-Null
Assert-That ((Read-Pointer (Join-Path $case.root 'current.txt')) -ceq 'a1') 'Transactional rollback failed to select previous.'
$results.production_r03_rollback_transaction=$true
$marker=Join-Path $case.root 'r03-protocol-required'
[IO.File]::Delete($marker)
Must-Fail { & $Lifecycle -Action status -InstallRoot $case.root -DataRoot $case.data } 'without its protocol marker'
& $Lifecycle -Action recover -InstallRoot $case.root -DataRoot $case.data | Out-Null
Assert-That ([IO.File]::Exists($marker)) 'Explicit recover did not repair a missing bootstrap marker.'
[IO.File]::Delete((Join-Path $case.root 'r03-state.json'))
Must-Fail { & $Lifecycle -Action status -InstallRoot $case.root -DataRoot $case.data } 'authority is missing'
$results.production_r03_bootstrap_fails_closed=$true
if ($env:R03_PRODUCT_ONLY) { $results | ConvertTo-Json -Compress; return }

# Fault and native process-crash matrix: A->A, A->B retained A, A->B revoked A.
$points=@('verify.before','verify.after','prepare.before','prepare.after','reverify.before','reverify.after',
    'receipt.before','receipt.after','commit.before','commit.after','mirror-trust.before','mirror-trust.after',
    'mirror-previous.before','mirror-previous.after','mirror-current.before','mirror-current.after','cleanup.before','cleanup.after')
$policies=@(
    @{name='same';version='a3';pins=$pinsA;revoked=$false},
    @{name='keep';version='b3';pins=$pinsAB;revoked=$false},
    @{name='revoke';version='b3';pins=$pinsB;revoked=$true}
)
$matrix=0
foreach ($policy in $policies) {
    foreach ($mode in @('error','crash')) {
        for ($index=0;$index -lt $points.Count;$index++) {
            $point=$points[$index]
            $case=Adopt-Case ($policy.name+'-'+$mode+'-'+$index)
            if ($mode -ceq 'crash') {
                $failed=Child $case upgrade $policy.version $policy.pins $point $true
                Assert-That ($failed.code -eq 73) ("Crash injection failed: $point "+$failed.error)
            }
            else {
                Must-Fail { Invoke-R03Probe -Action upgrade -Version $policy.version -Pins $policy.pins -Fault $point } 'injected fault'
            }
            Restore-ProbeContext $case
            $state=Invoke-R03Probe -Action status
            $accepted=$index -ge 3
            if (-not $accepted) {
                Assert-That ($state.phase -ceq 'ready' -and $state.current -ceq 'a2' -and $state.previous -ceq 'a1') 'Pre-acceptance failure changed selection.'
                Assert-That (@($state.pins) -contains $fixture.pin_a) 'Unaccepted request revoked A.'
            }
            else {
                if ($policy.revoked) { Assert-That (@($state.pins) -notcontains $fixture.pin_a) 'Accepted revocation was undone.' }
                if ($state.phase -ceq 'prepared') {
                    Assert-That ($state.current -ceq 'a2' -and $state.previous -ceq 'a1') 'Pending state destroyed old rollback history.'
                    Must-Fail { Invoke-R03Probe -Action authorize-start } 'recovery required'
                    Must-Fail { Invoke-R03Probe -Action authorize-rollback } 'recovery required'
                }
                elseif ($state.phase -ceq 'committed') {
                    Assert-That ($state.current -ceq $policy.version -and $state.previous -ceq 'a2') 'Committed authority mixed generations.'
                }
            }
            if ($accepted) {
                $recovered=Child $case recover
                Assert-That ($recovered.code -eq 0) ("Recovery failed: $point "+$recovered.error)
            }
            else {
                Invoke-R03Probe -Action upgrade -Version $policy.version -Pins $policy.pins | Out-Null
            }
            Restore-ProbeContext $case
            $final=Invoke-R03Probe -Action status
            Assert-That ($final.phase -ceq 'ready' -and $final.generation -eq 1 -and $final.current -ceq $policy.version -and $final.previous -ceq 'a2') 'Recovery selected inconsistent state.'
            $before=[IO.File]::ReadAllText($R03StatePath)
            Invoke-R03Probe -Action recover | Out-Null
            Invoke-R03Probe -Action upgrade -Version $policy.version -Pins $policy.pins | Out-Null
            Assert-That ([IO.File]::ReadAllText($R03StatePath) -ceq $before) 'Retry altered generation/previous.'
            Assert-That ((Invoke-R03Probe -Action authorize-start) -ceq $policy.version) 'Wrong launch authorization.'
            if ($policy.revoked) {
                Must-Fail { Invoke-R03Probe -Action authorize-rollback } 'not a trusted publisher|no publishers authorized'
                Assert-That (@($final.pins) -notcontains $fixture.pin_a) 'Recovery added revoked signer.'
            }
            else { Assert-That ((Invoke-R03Probe -Action authorize-rollback) -ceq 'a2') 'Allowed signer rollback lost.' }
            Assert-Data $case
            $matrix++
        }
    }
}
$results.fault_matrix_cases=$matrix
$results.fault_matrix_points_per_policy=$points.Count
$results.fault_matrix_revocation_safe=$true
$results.fault_matrix_repeated_recovery_idempotent=$true

# Bootstrap is also guarded. A marker without authority never re-imports legacy.
$adoption=0
foreach ($point in @('marker.before','marker.after','adopt.before','adopt.after')) {
    $case=New-Case ('adopt-'+$adoption)
    $failed=Child $case adopt '' $pinsA $point $true
    Assert-That ($failed.code -eq 73) 'Adoption crash not injected.'
    Restore-ProbeContext $case
    if ($point -ceq 'marker.before') {
        Assert-That (-not [IO.File]::Exists($R03MarkerPath)) 'Marker existed before acceptance.'
        Invoke-R03Probe -Action adopt -Pins $pinsA | Out-Null
    }
    elseif ($point -ceq 'adopt.after') { Assert-That ((Invoke-R03Probe -Action status).phase -ceq 'ready') 'Published adoption lost.' }
    else {
        Must-Fail { Invoke-R03Probe -Action status } 'missing'
        Must-Fail { Invoke-R03Probe -Action adopt -Pins $pinsA } 'one-time'
    }
    $adoption++
}
$results.adoption_crash_points=$adoption
$results.missing_authority_never_falls_back=$true

# Native sharing-denied handles: authority, receipt, mirrors; release then retry.
$lockCases=0
foreach ($name in @('.r03-probe-state.json','verification-policy.b3.json','trusted-publishers.json','previous.txt','current.txt')) {
    foreach ($shareName in @('Read','None')) {
        $case=Adopt-Case ('lock-'+$lockCases)
        $target=Join-Path $case.root $name
        if (-not [IO.File]::Exists($target)) { [IO.File]::WriteAllText($target,'preexisting-receipt') }
        $share=if ($shareName -ceq 'Read') { [IO.FileShare]::Read } else { [IO.FileShare]::None }
        $held=[IO.File]::Open($target,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
        try { Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB } }
        finally { $held.Dispose() }
        $state=Invoke-R03Probe -Action status
        if ($name -ceq '.r03-probe-state.json') {
            Assert-That ($state.phase -ceq 'ready' -and @($state.pins) -contains $fixture.pin_a) 'Failed authority publication accepted revocation.'
            Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB | Out-Null
        }
        else {
            Assert-That (@($state.pins) -notcontains $fixture.pin_a) 'Later I/O failure restored revoked policy.'
            Invoke-R03Probe -Action recover | Out-Null
        }
        Assert-That ((Invoke-R03Probe -Action status).current -ceq 'b3') 'Handle failure did not recover.'
        Assert-Data $case
        $lockCases++
    }
}
$results.native_locked_file_cases=$lockCases

# State/marker/destination types never become containers or recovery sources.
$typeCases=0
foreach ($name in @('.r03-probe-state.json','.r03-probe-required','current.txt','previous.txt','trusted-publishers.json')) {
    $case=Adopt-Case ('type-'+$typeCases)
    $target=Join-Path $case.root $name
    [IO.File]::Delete($target)
    [IO.Directory]::CreateDirectory($target) | Out-Null
    [IO.File]::WriteAllText((Join-Path $target 'retained.txt'),'retained')
    Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB }
    Assert-That (@(Get-ChildItem -LiteralPath $target -Force).Count -eq 1) 'State file was moved into a directory.'
    Assert-Data $case
    $typeCases++
}
$case=Adopt-Case 'junction'
[IO.File]::Delete($R03StatePath)
$outside=Join-Path $WorkRoot 'outside'
New-Item -ItemType Directory -Path $outside | Out-Null
[IO.File]::WriteAllText((Join-Path $outside 'retained.txt'),'retained')
New-Item -ItemType Junction -Path $R03StatePath -Target $outside | Out-Null
Must-Fail { Invoke-R03Probe -Action recover } 'regular file'
Assert-That (@(Get-ChildItem -LiteralPath $outside -Force).Count -eq 1) 'Recovery followed junction.'
$results.invalid_target_cases=$typeCases+1

# Same ID/content binding, stale generation, pending intent, and manifest drift.
$case=Adopt-Case 'intent-conflict'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB -Fault 'prepare.after' } 'injected'
$before=[IO.File]::ReadAllText($R03StatePath)
Must-Fail { Invoke-R03Probe -Action upgrade -Version a3 -Pins $pinsA -Transaction 'txn-2' } 'Pending intent'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsAB } 'conflicting contents'
Assert-That ([IO.File]::ReadAllText($R03StatePath) -ceq $before) 'Conflicting request overwrote pending policy.'
Invoke-R03Probe -Action recover | Out-Null
Must-Fail { Invoke-R03Probe -Action upgrade -Version a3 -Pins $pinsA -Transaction 'txn-stale' -BaseGeneration 0 } 'Stale base generation'
$results.conflicting_and_stale_intents_rejected=$true
$case=Adopt-Case 'same-version-policy'
$before=[IO.File]::ReadAllText($R03StatePath)
Must-Fail { Invoke-R03Probe -Action upgrade -Version a2 -Pins $pinsAB } 'separate explicit operation'
Assert-That ([IO.File]::ReadAllText($R03StatePath) -ceq $before) 'Same-version policy update erased previous.'
$results.same_version_policy_change_not_guessed=$true

$case=Adopt-Case 'candidate-damage'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB -Fault 'prepare.after' } 'injected'
$payload=Join-Path $case.root 'versions/b3/module.py'
$original=[IO.File]::ReadAllBytes($payload)
[IO.File]::WriteAllText($payload,'damaged after acceptance')
Must-Fail { Invoke-R03Probe -Action recover } 'hash mismatch'
$blocked=Invoke-R03Probe -Action status
Assert-That ($blocked.phase -ceq 'prepared' -and @($blocked.pins) -notcontains $fixture.pin_a) 'Damaged recovery resurrected A.'
Must-Fail { Invoke-R03Probe -Action authorize-start } 'recovery required'
[IO.File]::WriteAllBytes($payload,$original)
Invoke-R03Probe -Action recover | Out-Null
$results.post_acceptance_candidate_damage_stays_blocked=$true

$case=Adopt-Case 'repeated-recovery-crash'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB -Fault 'prepare.after' } 'injected'
foreach ($point in @('commit.after','mirror-previous.after')) {
    $again=Child $case recover '' @() $point $true
    Assert-That ($again.code -eq 73) 'Recovery crash point did not execute.'
    Restore-ProbeContext $case
    $state=Invoke-R03Probe -Action status
    Assert-That ($state.phase -ceq 'committed' -and $state.current -ceq 'b3' -and @($state.pins) -notcontains $fixture.pin_a) 'Repeated recovery changed authority.'
}
Invoke-R03Probe -Action recover | Out-Null
$results.recovery_itself_can_crash_and_retry=$true

$case=Adopt-Case 'untrusted-candidate'
$before=[IO.File]::ReadAllText($R03StatePath)
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsA } 'not a trusted publisher|no publishers authorized'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins @($fixture.pin_b) } 'Authenticode'
Assert-That ([IO.File]::ReadAllText($R03StatePath) -ceq $before) 'Rejected signer candidate changed effective policy.'
$results.manifest_and_launcher_signers_cannot_self_authorize=$true

# Corruption is diagnosed; no guess from mirrors or package-defined trust.
$corruptCases=0
foreach ($kind in @('missing','invalid-json','oversized','bad-digest','unsafe-version','extra-field','missing-intent','wrong-installation','scalar-pins')) {
    $case=Adopt-Case ('corrupt-'+$corruptCases)
    if ($kind -ceq 'missing') { [IO.File]::Delete($R03StatePath) }
    elseif ($kind -ceq 'invalid-json') { [IO.File]::WriteAllText($R03StatePath,'{broken') }
    elseif ($kind -ceq 'oversized') { [IO.File]::WriteAllText($R03StatePath,('x'*70000)) }
    else {
        $envelope=Get-Content -LiteralPath $R03StatePath -Raw | ConvertFrom-Json
        if ($kind -ceq 'bad-digest') { $envelope.sha256=('0'*64) }
        else {
            $value=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($envelope.payload)) | ConvertFrom-Json
            if ($kind -ceq 'unsafe-version') { $value.current='../escape' }
            elseif ($kind -ceq 'missing-intent') { $value.phase='prepared';$value.intent=$null }
            elseif ($kind -ceq 'wrong-installation') { $value.installation=('f'*64) }
            elseif ($kind -ceq 'scalar-pins') { $value.pins=$fixture.pin_a }
            else { $value | Add-Member -NotePropertyName command -NotePropertyValue 'must never execute' }
            $raw=[Text.UTF8Encoding]::new($false).GetBytes(($value | ConvertTo-Json -Depth 8 -Compress))
            $envelope.payload=[Convert]::ToBase64String($raw);$envelope.sha256=Get-R03Hash $raw
        }
        [IO.File]::WriteAllText($R03StatePath,($envelope | ConvertTo-Json -Compress))
    }
    Must-Fail { Invoke-R03Probe -Action recover }
    Must-Fail { Invoke-R03Probe -Action authorize-start }
    $corruptCases++
}
$results.corrupt_or_unsafe_state_cases=$corruptCases

# Borrowing the actual lifecycle lock blocks the prototype and the actual tool.
$case=Adopt-Case 'ownership'
$held=[IO.File]::Open((Join-Path $case.data '.noyra-lifecycle.lock'),[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
    $contender=Child $case status
    Assert-That ($contender.code -ne 0) 'Concurrent process bypassed lifecycle lock.'
    Must-Fail { & $Lifecycle -Action rollback -InstallRoot $case.root -DataRoot $case.data }
}
finally { $held.Dispose() }
$results.real_cross_process_and_legacy_lock_enforced=$true

# Deliberate unsigned development stays explicit; strictness cannot downgrade.
$case=New-Case 'development' -Development
Invoke-R03Probe -Action adopt -Pins @() -Strict:$false | Out-Null
Invoke-R03Probe -Action upgrade -Version u1 -Pins @() -Strict:$false | Out-Null
Assert-That ((Invoke-R03Probe -Action status).strict -eq $false) 'Development profile changed implicitly.'
$case=Adopt-Case 'strict-no-downgrade'
Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $FixtureJson) 'u1') -Destination (Join-Path $case.root 'versions/u1') -Recurse
Must-Fail { Invoke-R03Probe -Action upgrade -Version u1 -Pins $pinsB -Strict:$false } 'signed manifest must require'
$case=New-Case 'development-to-signed' -Development
Invoke-R03Probe -Action adopt -Pins @() -Strict:$false | Out-Null
Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB -Strict:$false | Out-Null
Assert-That ((Invoke-R03Probe -Action status).strict -eq $true) 'Signed package did not persist strict recovery.'
Must-Fail { Invoke-R03Probe -Action authorize-rollback } 'signed manifest must require'
$results.explicit_development_and_strict_no_downgrade=$true

# PREPARED changes authority immediately but keeps old mirrors until commit.
# Supported lifecycle commands use authority; executing an old copied script is
# outside the file protocol and must be controlled by deployment/OS versioning.
$case=Adopt-Case 'legacy-mirror-gate'
Must-Fail { Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB -Fault 'prepare.after' } 'injected'
Must-Fail { Invoke-R03Probe -Action authorize-start } 'recovery required'
Assert-That ((Read-Pointer (Join-Path $case.root 'current.txt')) -ceq 'a2') 'Prepared protocol unexpectedly rewrote mirrors.'
Assert-That (@(Read-PublisherTrustPolicy) -contains $fixture.pin_a) 'Legacy mirror observation changed.'
Invoke-R03Probe -Action recover | Out-Null
Assert-That ((Invoke-R03Probe -Action status).current -ceq 'b3') 'Authority did not overwrite stale mirrors.'
$results.legacy_mirror_remains_non_authoritative_during_prepared=$true
$results.prototype_is_not_deployable_without_entry_gate=$true

# Mirrors alone cannot undo the authority's revocation after successful cleanup.
$case=Adopt-Case 'stale-mirrors'
Invoke-R03Probe -Action upgrade -Version b3 -Pins $pinsB | Out-Null
Write-PointerAtomic $currentPointer 'a2'
Write-PublisherTrustPolicy $pinsA
Assert-That ((Invoke-R03Probe -Action authorize-start) -ceq 'b3') 'New entry trusted stale mirrors.'
Assert-That (@((Invoke-R03Probe -Action status).pins) -notcontains $fixture.pin_a) 'Stale trust mirror resurrected A.'
$results.compatibility_mirrors_never_authoritative=$true
$results.launchers_executed=0
$results.certificates_imported=0
$results.power_shell_version=$PSVersionTable.PSVersion.ToString()
$results | ConvertTo-Json -Depth 6 -Compress
