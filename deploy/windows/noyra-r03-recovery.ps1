# Windows install-state transaction helpers. This file is included in the signed
# package and dot-sourced only by noyra-lifecycle.ps1.
Set-StrictMode -Version Latest

function Get-R03Hash([byte[]]$Bytes) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

function Get-R03InstallationBinding {
    $identity = $InstallRoot.ToUpperInvariant() + '|' + $DataRoot.ToUpperInvariant()
    return Get-R03Hash ([Text.UTF8Encoding]::new($false).GetBytes($identity))
}

function Assert-R03Keys($Value, [string[]]$Keys) {
    if ($null -eq $Value) { throw 'R03 authority object is missing.' }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    if (($actual -join ',') -cne (@($Keys | Sort-Object) -join ',')) {
        throw 'R03 authority fields are invalid.'
    }
}

function Assert-R03State($State) {
    Assert-R03Keys $State @('format', 'installation', 'generation', 'phase', 'current', 'previous', 'pins', 'strict', 'intent')
    if ($State.format -cne 'noyra-r03-state/v1' -or
        $State.installation -cne (Get-R03InstallationBinding) -or
        ($State.generation -isnot [int] -and $State.generation -isnot [long]) -or
        $State.generation -lt 0 -or $State.generation -gt 100000 -or
        $State.phase -cnotin @('ready', 'prepared', 'committed') -or $State.strict -isnot [bool]) {
        throw 'R03 authority contract is invalid.'
    }
    if ($null -ne $State.current) { Assert-SafeSegment ([string]$State.current) 'R03 current version' }
    if ($null -ne $State.previous) { Assert-SafeSegment ([string]$State.previous) 'R03 previous version' }
    if ($State.pins -isnot [array]) { throw 'R03 publisher pins must be an array.' }
    $rawPins = @($State.pins)
    if (@($rawPins | Where-Object { $_ -isnot [string] }).Count -gt 0) {
        throw 'R03 publisher pins must contain only strings.'
    }
    $pins = @(Normalize-Thumbprints $rawPins)
    if ($pins.Count -gt 128 -or ($State.strict -and $pins.Count -eq 0) -or
        (($rawPins -join ',') -cne ($pins -join ','))) {
        throw 'R03 publisher pins are invalid.'
    }
    $State.pins = @($pins)
    if ($null -eq $State.intent) {
        if ($State.phase -cne 'ready' -or $null -eq $State.current -or $State.generation -ne 0) {
            throw 'R03 authority is missing a required transaction intent.'
        }
        return
    }
    $intent = $State.intent
    Assert-R03Keys $intent @('id', 'kind', 'version', 'manifest_hash', 'strict', 'previous', 'next_generation')
    Assert-SafeSegment ([string]$intent.id) 'R03 intent id'
    Assert-SafeSegment ([string]$intent.version) 'R03 intent version'
    if ($null -ne $intent.previous) { Assert-SafeSegment ([string]$intent.previous) 'R03 intent previous version' }
    if ($intent.kind -cnotin @('install', 'upgrade', 'rollback') -or
        $intent.manifest_hash -isnot [string] -or $intent.manifest_hash -cnotmatch '^[0-9a-f]{64}$' -or
        $intent.strict -isnot [bool] -or $intent.strict -ne $State.strict -or
        ($intent.next_generation -isnot [int] -and $intent.next_generation -isnot [long])) {
        throw 'R03 transaction intent is invalid.'
    }
    if ($State.phase -ceq 'prepared') {
        if ($intent.next_generation -ne ($State.generation + 1) -or $intent.previous -cne $State.current) {
            throw 'R03 prepared generation binding is invalid.'
        }
    }
    elseif ($intent.next_generation -ne $State.generation -or
        $intent.version -cne $State.current -or $intent.previous -cne $State.previous) {
        throw 'R03 committed generation binding is invalid.'
    }
}

function Write-R03Text([string]$Path, [string]$Text) {
    Assert-StateFileTarget $Path
    $temporary = Join-Path $InstallRoot ('.r03-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Publish-StateFile $temporary $Path
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Write-R03Marker {
    Assert-StateFileTarget $script:r03MarkerPath
    if ([IO.File]::Exists($script:r03MarkerPath)) {
        $item = Get-Item -LiteralPath $script:r03MarkerPath -Force
        if ($item.Length -gt 128 -or [IO.File]::ReadAllText($script:r03MarkerPath) -cne 'noyra-r03-required/v1') {
            throw 'R03 protocol marker is invalid.'
        }
        return
    }
    Write-R03Text $script:r03MarkerPath 'noyra-r03-required/v1'
}

function Write-R03State($State) {
    Assert-R03State $State
    $payload = [Text.UTF8Encoding]::new($false).GetBytes(($State | ConvertTo-Json -Depth 10 -Compress))
    $envelope = [ordered]@{
        format = 'noyra-r03-envelope/v1'
        payload = [Convert]::ToBase64String($payload)
        sha256 = Get-R03Hash $payload
    }
    Write-R03Text $script:r03StatePath ($envelope | ConvertTo-Json -Compress)
}

function Read-R03State([switch]$RepairMarker) {
    foreach ($path in @($script:r03MarkerPath, $script:r03StatePath)) { Assert-StateFileTarget $path }
    $hasMarker = [IO.File]::Exists($script:r03MarkerPath)
    $hasState = [IO.File]::Exists($script:r03StatePath)
    if (-not $hasMarker -and -not $hasState) { return $null }
    if (-not $hasState) {
        if ([IO.File]::Exists($script:r03TransitionPath)) { return $null }
        throw 'R03 protocol marker exists but authority is missing; legacy fallback is forbidden.'
    }
    if (-not $hasMarker -and -not $RepairMarker -and -not [IO.File]::Exists($script:r03TransitionPath)) { throw 'R03 authority exists without its protocol marker.' }
    $stateItem = Get-Item -LiteralPath $script:r03StatePath -Force
    if ($stateItem.Length -gt 65536) { throw 'R03 authority exceeds its size limit.' }
    if ($hasMarker) {
        $markerItem = Get-Item -LiteralPath $script:r03MarkerPath -Force
        if ($markerItem.Length -gt 128 -or [IO.File]::ReadAllText($script:r03MarkerPath) -cne 'noyra-r03-required/v1') {
            throw 'R03 protocol marker is invalid.'
        }
    }
    try {
        $envelope = [IO.File]::ReadAllText($script:r03StatePath) | ConvertFrom-Json
        Assert-R03Keys $envelope @('format', 'payload', 'sha256')
        if ($envelope.format -cne 'noyra-r03-envelope/v1' -or
            $envelope.payload -isnot [string] -or $envelope.sha256 -isnot [string]) { throw 'R03 envelope is invalid.' }
        $payload = [Convert]::FromBase64String($envelope.payload)
        if ($payload.Length -gt 32768 -or (Get-R03Hash $payload) -cne $envelope.sha256) { throw 'R03 authority digest is invalid.' }
        $state = [Text.UTF8Encoding]::new($false, $true).GetString($payload) | ConvertFrom-Json
        Assert-R03State $state
    }
    catch { throw 'R03 authority is invalid; legacy fallback is forbidden.' }
    if (-not $hasMarker -and $RepairMarker) { Write-R03Marker }
    return $state
}

function Read-R03Transition {
    Assert-StateFileTarget $script:r03TransitionPath
    if (-not [IO.File]::Exists($script:r03TransitionPath)) { return $null }
    $item = Get-Item -LiteralPath $script:r03TransitionPath -Force
    if ($item.Length -gt 65536) { throw 'R03 transition record exceeds its size limit.' }
    try {
        $record = [IO.File]::ReadAllText($script:r03TransitionPath) | ConvertFrom-Json
        Assert-R03Keys $record @('format','installation','kind','version','manifest_hash','strict','pins','prior_state','prior_sha256')
        if ($record.format -cne 'noyra-r03-transition/v1' -or
            $record.installation -cne (Get-R03InstallationBinding) -or
            $record.kind -cnotin @('install','upgrade','rollback') -or
            $record.version -isnot [string] -or $record.manifest_hash -isnot [string] -or
            $record.manifest_hash -cnotmatch '^[0-9a-f]{64}$' -or $record.strict -isnot [bool] -or
            $record.prior_state -isnot [string] -or $record.prior_sha256 -isnot [string]) {
            throw 'R03 transition record fields are invalid.'
        }
        Assert-SafeSegment $record.version 'R03 transition version'
        if ($record.pins -isnot [array]) { throw 'R03 transition pins must be an array.' }
        $rawPins = @($record.pins)
        if (@($rawPins | Where-Object { $_ -isnot [string] }).Count -gt 0) { throw 'R03 transition pins must contain only strings.' }
        $pins = @(Normalize-Thumbprints $rawPins)
        if (($rawPins -join ',') -cne ($pins -join ',')) { throw 'R03 transition pins are invalid.' }
        $priorBytes = [Convert]::FromBase64String($record.prior_state)
        if ($priorBytes.Length -gt 32768 -or (Get-R03Hash $priorBytes) -cne $record.prior_sha256) { throw 'R03 transition prior state digest is invalid.' }
        $priorState = [Text.UTF8Encoding]::new($false, $true).GetString($priorBytes) | ConvertFrom-Json
        Assert-R03State $priorState
        if ($priorState.phase -cne 'ready') { throw 'R03 transition prior state is not ready.' }
        return [pscustomobject][ordered]@{
            format = $record.format; installation = $record.installation; kind = $record.kind
            version = $record.version; manifest_hash = $record.manifest_hash; strict = $record.strict
            pins = @($pins); prior_state = $priorState
        }
    }
    catch { throw 'R03 transition record is invalid; legacy fallback is forbidden.' }
}

function Test-R03PreparedTransition($State, $Transition) {
    if ($null -eq $State -or $null -eq $Transition -or $State.phase -cne 'prepared' -or $null -eq $State.intent) { return $false }
    return ($State.generation -eq $Transition.prior_state.generation -and
        $State.current -ceq $Transition.prior_state.current -and
        $State.intent.next_generation -eq ($Transition.prior_state.generation + 1) -and
        $State.intent.previous -ceq $Transition.prior_state.current -and
        $State.intent.kind -ceq $Transition.kind -and $State.intent.version -ceq $Transition.version -and
        $State.intent.manifest_hash -ceq $Transition.manifest_hash -and $State.strict -eq $Transition.strict -and
        (@($State.pins) -join ',') -ceq (@($Transition.pins) -join ','))
}
function Test-R03TransitionAccepted($State, $Transition) {
    if ($null -eq $State -or $null -eq $Transition -or $State.phase -cne 'ready' -or $null -eq $State.intent) { return $false }
    return ($State.generation -eq ($Transition.prior_state.generation + 1) -and
        $State.current -ceq $Transition.version -and $State.previous -ceq $Transition.prior_state.current -and
        $State.intent.kind -ceq $Transition.kind -and $State.intent.version -ceq $Transition.version -and
        $State.intent.manifest_hash -ceq $Transition.manifest_hash -and $State.strict -eq $Transition.strict -and
        (@($State.pins) -join ',') -ceq (@($Transition.pins) -join ','))
}
function Write-R03Transition($PriorState, [string]$Kind, [string]$Version, [string]$Digest, [bool]$Strict, [string[]]$Pins) {
    Assert-R03State $PriorState
    if ($PriorState.phase -cne 'ready') { throw 'R03 transition requires a ready prior state.' }
    Assert-SafeSegment $Kind 'R03 transition kind'
    Assert-SafeSegment $Version 'R03 transition version'
    if ($Digest -notmatch '^[0-9a-f]{64}$') { throw 'R03 transition manifest digest is invalid.' }
    $normalized = @(Normalize-Thumbprints $Pins)
    $priorBytes = [Text.UTF8Encoding]::new($false).GetBytes(($PriorState | ConvertTo-Json -Depth 10 -Compress))
    $record = [ordered]@{
        format = 'noyra-r03-transition/v1'; installation = Get-R03InstallationBinding; kind = $Kind
        version = $Version; manifest_hash = $Digest; strict = $Strict; pins = @($normalized)
        prior_state = [Convert]::ToBase64String($priorBytes); prior_sha256 = Get-R03Hash $priorBytes
    }
    Write-R03Text $script:r03TransitionPath ($record | ConvertTo-Json -Depth 10 -Compress)
}

function Remove-R03Transition {
    Assert-StateFileTarget $script:r03TransitionPath
    if ([IO.File]::Exists($script:r03TransitionPath)) { [IO.File]::Delete($script:r03TransitionPath) }
}

function Recover-R03PreAcceptance {
    $transition = Read-R03Transition
    if ($null -eq $transition) { return $null }
    Write-R03State $transition.prior_state
    Write-R03Marker
    Write-R03Mirrors $transition.prior_state
    Remove-R03Transition
    return $transition.prior_state
}

function Test-R03ProtocolFiles {
    Assert-StateFileTarget $script:r03MarkerPath
    Assert-StateFileTarget $script:r03StatePath
    Assert-StateFileTarget $script:r03TransitionPath
    return ([IO.File]::Exists($script:r03MarkerPath) -or [IO.File]::Exists($script:r03StatePath) -or [IO.File]::Exists($script:r03TransitionPath))
}

function Get-R03LegacyStrict([string]$Current, [string[]]$LegacyPins) {
    if ([string]::IsNullOrWhiteSpace($Current)) { return $false }
    $receipt = Join-Path $InstallRoot ("verification-policy.$Current.json")
    if ([IO.File]::Exists($receipt)) {
        return Read-RecoverySignatureRequirement $Current
    }
    return (@($LegacyPins).Count -gt 0)
}

function Get-OrAdopt-R03State([switch]$RepairMarker) {
    $state = Read-R03State -RepairMarker:$RepairMarker
    $transition = Read-R03Transition
    if ($null -ne $transition -and $null -eq $state) {
        $state = Recover-R03PreAcceptance
    }
    elseif ($null -ne $transition -and $state.phase -ceq 'ready') {
        if (Test-R03TransitionAccepted $state $transition) { Remove-R03Transition }
        else { $state = Recover-R03PreAcceptance }
    }
    if ($null -ne $state) {
        $script:TrustedPublisherThumbprints = @($state.pins)
        if ($state.phase -ceq 'ready') { Write-R03Mirrors $state }
        return $state
    }
    $current = Read-Pointer $script:currentPointer
    $previous = Read-Pointer $script:previousPointer
    if ($null -eq $current) {
        if ($null -ne $previous) { throw 'Legacy previous pointer exists without a current version.' }
        return $null
    }
    # Adoption imports only operator-controlled pointers, receipt and saved trust.
    # Every selected legacy package is independently verified before authority
    # or the protocol marker is published.
    $legacyPins = @(Read-PublisherTrustPolicy)
    $requestedPins = @($script:R03RequestedPins)
    $script:TrustedPublisherThumbprints = @($legacyPins)
    $strict = Get-R03LegacyStrict $current $legacyPins
    foreach ($selected in @($current, $previous | Where-Object { $null -ne $_ })) {
        $selectedRoot = Join-Path $script:versions ([string]$selected)
        if (-not [IO.Directory]::Exists($selectedRoot)) {
            throw 'Legacy R03 selection references a missing version directory.'
        }
        $manifest = Verify-Package -Root $selectedRoot -ForceSignature:$strict
        if ([string]$manifest.version -cne [string]$selected) {
            throw 'Legacy R03 pointer does not match the verified package version.'
        }
    }
    $state = [pscustomobject][ordered]@{
        format = 'noyra-r03-state/v1'
        installation = Get-R03InstallationBinding
        generation = 0
        phase = 'ready'
        current = $current
        previous = $previous
        pins = @($legacyPins)
        strict = [bool]$strict
        intent = $null
    }
    Write-R03State $state
    Write-R03Marker
    Write-R03Mirrors $state
    if ($requestedPins.Count) { $script:TrustedPublisherThumbprints = @($requestedPins) }
    else { $script:TrustedPublisherThumbprints = @($legacyPins) }
    return $state
}
function Set-R03VerificationPins([object[]]$Pins, [switch]$AllowNarrowing) {
    $normalized = @(Normalize-Thumbprints $Pins)
    if ($AllowNarrowing -and $script:R03RequestedPins.Count -gt 0) {
        foreach ($pin in $script:R03RequestedPins) {
            if ($normalized -notcontains $pin) { throw 'Per-call publisher pins cannot add trust outside the R03 authority.' }
        }
        $normalized = @($script:R03RequestedPins)
    }
    $script:TrustedPublisherThumbprints = @($normalized)
}

function Get-R03ManifestDigest([string]$Version) {
    Assert-SafeSegment $Version 'R03 manifest version'
    $path = Join-Path (Join-Path $script:versions $Version) 'noyra-package.json'
    Assert-StateFileTarget $path
    if (-not [IO.File]::Exists($path)) { throw 'R03 candidate manifest is missing.' }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-R03Receipt([string]$Version, [bool]$Strict) {
    Write-InstalledVerificationPolicy $Version $Strict
}

function Remove-R03Mirror([string]$Path) {
    Assert-StateFileTarget $Path
    if ([IO.File]::Exists($Path)) { [IO.File]::Delete($Path) }
}

function Write-R03Mirrors($State) {
    if (@($State.pins).Count) { Write-PublisherTrustPolicy @($State.pins) }
    else { Remove-R03Mirror $script:publisherTrustPath }
    if ($null -ne $State.previous) { Write-PointerAtomic $script:previousPointer ([string]$State.previous) }
    else { Remove-R03Mirror $script:previousPointer }
    Write-PointerAtomic $script:currentPointer ([string]$State.current)
}

function Write-R03TransitionMirrors {
    # Gate older lifecycle copies before accepting a new policy. All normal
    # legacy selectors are neutralized before the incompatible trust projection
    # is published. The lifecycle lock serializes supported callers.
    Write-R03Text $script:previousPointer '..'
    Write-R03Text $script:currentPointer '..'
    Write-R03Text $script:publisherTrustPath '{"format":"noyra-r03-transition/v1","thumbprints":[]}'
}

function Complete-R03Recovery($State) {
    if ($State.phase -ceq 'prepared') {
        $script:TrustedPublisherThumbprints = @($State.pins)
        $manifest = Verify-Package -Root (Join-Path $script:versions $State.intent.version) -ForceSignature:([bool]$State.intent.strict)
        if ([string]$manifest.version -cne [string]$State.intent.version -or
            (Get-R03ManifestDigest ([string]$State.intent.version)) -cne [string]$State.intent.manifest_hash) {
            throw 'Prepared R03 candidate no longer matches its verified intent; authority remains blocked.'
        }
        Write-R03Receipt ([string]$State.intent.version) ([bool]$State.intent.strict)
        $State.current = [string]$State.intent.version
        $State.previous = $State.intent.previous
        $State.generation = [long]$State.intent.next_generation
        $State.phase = 'committed'
        Write-R03State $State
    }
    if ($State.phase -ceq 'committed') {
        $script:TrustedPublisherThumbprints = @($State.pins)
        $manifest = Verify-Package -Root (Join-Path $script:versions $State.current) -ForceSignature:([bool]$State.strict)
        if ([string]$manifest.version -cne [string]$State.current -or
            (Get-R03ManifestDigest ([string]$State.current)) -cne [string]$State.intent.manifest_hash) {
            throw 'Committed R03 selection no longer matches its verified intent; authority remains blocked.'
        }
        Write-R03Mirrors $State
        $State.phase = 'ready'
        Write-R03State $State
        Remove-R03Transition
    }
    return $State
}

function New-R03TransactionId([string]$Kind, [long]$Generation, [string]$Version, [string]$Digest, [bool]$Strict, [string[]]$Pins) {
    $canonical = @($Kind, [string]$Generation, $Version, $Digest, [string]$Strict, (@($Pins) -join ',')) -join '|'
    return Get-R03Hash ([Text.UTF8Encoding]::new($false).GetBytes($canonical))
}

function Invoke-R03CommitSelection($State, [string]$Kind, [string]$Version, [string]$Digest, [bool]$Strict, [string[]]$Pins) {
    $Pins = @(Normalize-Thumbprints $Pins)
    if ($null -eq $State) {
        $State = [pscustomobject][ordered]@{
            format = 'noyra-r03-state/v1'; installation = Get-R03InstallationBinding; generation = 0; phase = 'ready'
            current = $null; previous = $null; pins = @(); strict = $false; intent = $null
        }
    }
    if ($State.phase -cne 'ready') { throw 'R03 recovery is required before another installation transaction.' }
    if ($Version -ceq [string]$State.current) {
        if ([bool]$State.strict -ne $Strict -or (@($State.pins) -join ',') -cne (@($Pins) -join ',')) {
            throw 'Same-version publisher policy change requires a separate explicit operation.'
        }
        return $State
    }
    if ($null -ne $State.current) {
        # This installation-bound transition record preserves the last
        # known-good state until PREPARED is durably accepted. It lets recovery
        # restore legacy mirrors if a crash happens before that acceptance point.
        Write-R03Transition $State $Kind $Version $Digest $Strict $Pins
        try { Write-R03TransitionMirrors }
        catch {
            try { Recover-R03PreAcceptance | Out-Null } catch {}
            throw
        }
    }
    $id = New-R03TransactionId $Kind ([long]$State.generation) $Version $Digest $Strict $Pins
    $State.phase = 'prepared'
    $State.pins = @($Pins)
    $State.strict = $Strict
    $State.intent = [pscustomobject][ordered]@{
        id = $id; kind = $Kind; version = $Version; manifest_hash = $Digest; strict = $Strict
        previous = $State.current; next_generation = ([long]$State.generation + 1)
    }
    try { Write-R03State $State }
    catch {
        if ($null -ne $State.intent.previous) {
            $published = $null
            try { $published = Read-R03State } catch {}
            $transition = $null
            try { $transition = Read-R03Transition } catch {}
            if (-not (Test-R03PreparedTransition $published $transition)) {
                try { Recover-R03PreAcceptance | Out-Null } catch {}
            }
        }
        throw
    }
    # The durable prepared state is the revocation acceptance point. A marker
    # or transition cleanup failure after this point never restores old trust.
    Write-R03Marker
    if ($null -ne $State.intent.previous) { Remove-R03Transition }
    return Complete-R03Recovery $State
}
function Invoke-R03InstallTransaction($State, [string]$Version, [bool]$RequireStrict) {
    Assert-SafeSegment $Version 'install version'
    $requestedPins = @()
    if ($script:R03RequestedPins.Count) { $requestedPins = @($script:R03RequestedPins) }
    elseif ($null -ne $State) { $requestedPins = @($State.pins) }
    else { $requestedPins = @($script:TrustedPublisherThumbprints) }
    $requestedPins = @(Normalize-Thumbprints $requestedPins)
    $script:TrustedPublisherThumbprints = @($requestedPins)
    $manifest = Verify-Package -Root (Join-Path $script:versions $Version) -ForceSignature:$RequireStrict
    if ([string]$manifest.version -cne $Version) { throw 'Install package version does not match its selected directory.' }
    $strict = [bool]($RequireStrict -or $manifest.signature_required)
    $pins = @()
    if ($strict) { $pins = @($requestedPins) }
    elseif ($null -ne $State) { $pins = @($State.pins) }
    $pins = @(Normalize-Thumbprints $pins)
    $digest = Get-R03ManifestDigest $Version
    $kind = if ($null -eq $State -or $null -eq $State.current) { 'install' } else { 'upgrade' }
    return Invoke-R03CommitSelection $State $kind $Version $digest $strict $pins
}

function Invoke-R03RollbackTransaction($State) {
    if ($null -eq $State -or $null -eq $State.previous) { throw 'No usable previous version is available.' }
    $State = Complete-R03Recovery $State
    Set-R03VerificationPins @($State.pins) -AllowNarrowing
    $target = [string]$State.previous
    $manifest = Verify-Package -Root (Join-Path $script:versions $target) -ForceSignature:([bool]($State.strict -or $RequireSignature))
    if ([string]$manifest.version -cne $target) { throw 'Rollback package version does not match its selection.' }
    $strict = [bool]($RequireSignature -or $manifest.signature_required)
    $digest = Get-R03ManifestDigest $target
    return Invoke-R03CommitSelection $State 'rollback' $target $digest $strict @($State.pins)
}

function Invoke-R03AuthorizeStart($State) {
    if ($null -eq $State -or $null -eq $State.current) { throw 'No installed Noyra version is selected.' }
    $State = Complete-R03Recovery $State
    if ($State.phase -cne 'ready') { throw 'R03 recovery is required before start.' }
    Set-R03VerificationPins @($State.pins) -AllowNarrowing
    $manifest = Verify-Package -Root (Join-Path $script:versions $State.current) -ForceSignature:([bool]($State.strict -or $RequireSignature))
    if ([string]$manifest.version -cne [string]$State.current) { throw 'Selected package version does not match its authority.' }
    if ($null -ne $State.intent -and [string]$State.intent.version -ceq [string]$State.current -and
        (Get-R03ManifestDigest ([string]$State.current)) -cne [string]$State.intent.manifest_hash) {
        throw 'Selected package differs from its committed R03 intent.'
    }
    return $manifest
}
