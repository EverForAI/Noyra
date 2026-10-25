# TEST-ONLY recovery protocol prototype. Never installed or invoked by Noyra.
# Single authoritative record; current/previous/trust remain compatibility mirrors.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Import-R03LifecycleHelpers([string]$Lifecycle) {
    $names = @('Assert-SafeSegment', 'Normalize-Thumbprints', 'Assert-StateFileTarget',
        'Publish-StateFile', 'Read-Pointer', 'Write-PointerAtomic',
        'Read-PublisherTrustPolicy', 'Write-PublisherTrustPolicy',
        'Write-InstalledVerificationPolicy', 'Read-RecoverySignatureRequirement',
        'Assert-PackageTree', 'Verify-ManifestSignature', 'Verify-Package')
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Lifecycle, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Lifecycle source does not parse.' }
    $definitions = @($ast.FindAll({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names
    }, $false))
    if ($definitions.Count -ne $names.Count) { throw 'Required real lifecycle helper missing.' }
    # Only trusted repository source is evaluated; no script is loaded from state/packages.
    foreach ($definition in $definitions) {
        . ([scriptblock]::Create($definition.Extent.Text.Replace(
            'function ' + $definition.Name + '(', 'function script:' + $definition.Name + '('
        ).Replace('function ' + $definition.Name + ' {', 'function script:' + $definition.Name + ' {')))
    }
}

function Initialize-R03Probe([string]$Root, [string]$Data) {
    $script:InstallRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $script:R03Data = [IO.Path]::GetFullPath($Data).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($script:InstallRoot -eq [IO.Path]::GetPathRoot($script:InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) -or
        $script:R03Data -eq $script:InstallRoot -or
        $script:R03Data.StartsWith($script:InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Probe installation/data roots must be separate non-root paths.'
    }
    foreach ($path in @($script:InstallRoot, $script:R03Data)) {
        $item = Get-Item -LiteralPath $path -Force
        while ($null -ne $item) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Probe root contains a reparse point.' }
            $item = $item.Parent
        }
    }
    $script:versions = Join-Path $script:InstallRoot 'versions'
    $script:publisherTrustPath = Join-Path $script:InstallRoot 'trusted-publishers.json'
    $script:currentPointer = Join-Path $script:InstallRoot 'current.txt'
    $script:previousPointer = Join-Path $script:InstallRoot 'previous.txt'
    $script:R03StatePath = Join-Path $script:InstallRoot '.r03-probe-state.json'
    $script:R03MarkerPath = Join-Path $script:InstallRoot '.r03-probe-required'
    $script:R03TransitionPath = Join-Path $script:InstallRoot '.r03-probe-transition.json'
    $identityText = $script:InstallRoot.ToUpperInvariant() + "|" + $script:R03Data.ToUpperInvariant()
    $script:R03Installation = Get-R03Hash ([Text.UTF8Encoding]::new($false).GetBytes($identityText))
    $script:R03Fault = ''; $script:R03Crash = $false
}

function Assert-R03Segment([object]$Value) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$' -or
        $Value -match '^(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$') {
        throw 'Unsafe prototype version/transaction segment.'
    }
}

function Assert-R03Keys($Value, [string[]]$Keys) {
    if ($null -eq $Value) { throw 'Missing protocol object.' }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    if (($actual -join ',') -cne (@($Keys | Sort-Object) -join ',')) { throw 'Protocol object fields invalid.' }
}

function Get-R03Hash([byte[]]$Bytes) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

function Invoke-R03Fault([string]$Point) {
    if ($script:R03Fault -ceq $Point) {
        if ($script:R03Crash) { [Environment]::Exit(73) }
        throw "injected fault: $Point"
    }
}

function Write-R03Text([string]$Path, [string]$Text) {
    Assert-StateFileTarget $Path
    $temporary = Join-Path $script:InstallRoot ('.r03-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Publish-StateFile $temporary $Path
    }
    finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Assert-R03State($State) {
    Assert-R03Keys $State @('format','installation','generation','phase','current','previous','pins','strict','intent')
    if ($State.format -cne 'noyra-r03-probe-state/v1' -or
        $State.installation -cne $script:R03Installation -or
        ($State.generation -isnot [int] -and $State.generation -isnot [long]) -or
        $State.generation -lt 0 -or $State.generation -gt 100000 -or
        $State.phase -cnotin @('ready','prepared','committed') -or $State.strict -isnot [bool]) {
        throw 'Protocol state contract invalid.'
    }
    Assert-R03Segment $State.current
    if ($null -ne $State.previous) { Assert-R03Segment $State.previous }
    if ($State.pins -isnot [array]) { throw 'Protocol trust must be an array.' }
    $pins = @($State.pins)
    if ($pins.Count -gt 128 -or ($State.strict -and $pins.Count -eq 0) -or
        (($pins -join ',') -cne (@(Normalize-Thumbprints $pins) -join ','))) { throw 'Protocol trust invalid.' }
    if ($null -ne $State.intent) {
        $intent = $State.intent
        Assert-R03Keys $intent @('id','version','manifest_hash','strict','previous','next_generation')
        Assert-R03Segment $intent.id; Assert-R03Segment $intent.version
        if ($null -ne $intent.previous) { Assert-R03Segment $intent.previous }
        if ($intent.manifest_hash -isnot [string] -or $intent.manifest_hash -cnotmatch '^[0-9a-f]{64}$' -or
            $intent.strict -isnot [bool] -or $intent.strict -ne $State.strict -or
            ($intent.next_generation -isnot [int] -and $intent.next_generation -isnot [long])) {
            throw 'Protocol intent invalid.'
        }
        if ($State.phase -ceq 'prepared') {
            if ($intent.next_generation -ne $State.generation + 1 -or $intent.previous -cne $State.current) {
                throw 'Prepared generation binding invalid.'
            }
        }
        elseif ($intent.next_generation -ne $State.generation -or
                $intent.version -cne $State.current -or $intent.previous -cne $State.previous) {
            throw 'Committed generation binding invalid.'
        }
    }
    elseif ($State.phase -cne 'ready' -or $State.generation -ne 0) { throw 'Missing durable intent.' }
}

function Write-R03State($State) {
    Assert-R03State $State
    $payload = [Text.UTF8Encoding]::new($false).GetBytes(($State | ConvertTo-Json -Depth 8 -Compress))
    $envelope = [ordered]@{format='noyra-r03-probe-envelope/v1'; payload=[Convert]::ToBase64String($payload); sha256=(Get-R03Hash $payload)}
    Write-R03Text $script:R03StatePath ($envelope | ConvertTo-Json -Compress)
}

function Read-R03State {
    foreach ($path in @($script:R03MarkerPath, $script:R03StatePath)) {
        Assert-StateFileTarget $path
        if (-not [IO.File]::Exists($path) -or (Get-Item -LiteralPath $path -Force).Length -gt 65536) {
            throw 'Protocol required state missing or oversized; no legacy fallback.'
        }
    }
    if ([IO.File]::ReadAllText($script:R03MarkerPath) -cne 'noyra-r03-probe-required/v1') { throw 'Protocol marker invalid.' }
    try {
        $envelope = [IO.File]::ReadAllText($script:R03StatePath) | ConvertFrom-Json
        Assert-R03Keys $envelope @('format','payload','sha256')
        if ($envelope.format -cne 'noyra-r03-probe-envelope/v1' -or
            $envelope.payload -isnot [string] -or $envelope.sha256 -isnot [string]) { throw 'Envelope invalid.' }
        $payload = [Convert]::FromBase64String($envelope.payload)
        if ($payload.Length -gt 32768 -or (Get-R03Hash $payload) -cne $envelope.sha256) { throw 'Envelope digest invalid.' }
        $state = [Text.UTF8Encoding]::new($false, $true).GetString($payload) | ConvertFrom-Json
        Assert-R03State $state
        return $state
    }
    catch { throw 'Protocol state invalid; no legacy fallback.' }
}

function Get-R03ManifestHash([string]$Version) {
    Assert-R03Segment $Version
    $path = Join-Path (Join-Path $script:versions $Version) 'noyra-package.json'
    Assert-StateFileTarget $path
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-R03Package([string]$Version, [string[]]$Pins, [bool]$Strict, [string]$Digest='') {
    Assert-R03Segment $Version
    $TrustedPublisherThumbprints = @($Pins)
    $RequireSignature = $false
    $manifest = Verify-Package -Root (Join-Path $script:versions $Version) -ForceSignature:$Strict
    if ($manifest.version -cne $Version -or ($Digest -and (Get-R03ManifestHash $Version) -cne $Digest)) {
        throw 'Package/intent digest or version mismatch.'
    }
    return $manifest
}

function Complete-R03Recovery($State) {
    if ($State.phase -ceq 'prepared') {
        Invoke-R03Fault 'reverify.before'
        Test-R03Package $State.intent.version @($State.pins) $State.strict $State.intent.manifest_hash | Out-Null
        Invoke-R03Fault 'reverify.after'
        Invoke-R03Fault 'receipt.before'
        Write-InstalledVerificationPolicy $State.intent.version $State.strict
        Invoke-R03Fault 'receipt.after'
        Invoke-R03Fault 'commit.before'
        $State.current = $State.intent.version
        $State.previous = $State.intent.previous
        $State.generation = $State.intent.next_generation
        $State.phase = 'committed'
        Write-R03State $State
        Invoke-R03Fault 'commit.after'
    }
    if ($State.phase -ceq 'committed') {
        Test-R03Package $State.current @($State.pins) $State.strict $State.intent.manifest_hash | Out-Null
        # These files are projections only. Failed repair cannot revert trust
        # or overwrite the authority with stale compatibility data.
        Invoke-R03Fault 'mirror-trust.before'
        if (@($State.pins).Count) { Write-PublisherTrustPolicy @($State.pins) }
        Invoke-R03Fault 'mirror-trust.after'
        Invoke-R03Fault 'mirror-previous.before'
        if ($null -ne $State.previous) { Write-PointerAtomic $script:previousPointer $State.previous }
        Invoke-R03Fault 'mirror-previous.after'
        Invoke-R03Fault 'mirror-current.before'
        Write-PointerAtomic $script:currentPointer $State.current
        Invoke-R03Fault 'mirror-current.after'
        Invoke-R03Fault 'cleanup.before'
        $State.phase = 'ready'
        Write-R03State $State
        Invoke-R03Fault 'cleanup.after'
    }
    return $State
}

function Invoke-R03Probe {
    param([string]$Action, [string]$Version='', [string[]]$Pins=@(), [bool]$Strict=$true,
        [string]$Transaction='txn-1', [long]$BaseGeneration=0,
        [string]$Fault='', [switch]$Crash)
    $script:R03Fault=$Fault; $script:R03Crash=[bool]$Crash
    # Same physical lock as the real lifecycle when using this fixture's DataRoot.
    $lock = [IO.File]::Open((Join-Path $script:R03Data '.noyra-lifecycle.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        if ($Action -ceq 'adopt') {
            Assert-StateFileTarget $script:R03MarkerPath; Assert-StateFileTarget $script:R03StatePath
            if ([IO.File]::Exists($script:R03MarkerPath) -or [IO.File]::Exists($script:R03StatePath)) {
                throw 'Adoption is one-time; marker cannot downgrade or re-import legacy trust.'
            }
            $current = Read-Pointer $script:currentPointer
            $previous = Read-Pointer $script:previousPointer
            if ($null -eq $current) { throw 'Missing legacy current.' }
            $normalized = @(Normalize-Thumbprints $Pins)
            $currentManifest = Test-R03Package $current $normalized $Strict
            $Strict = $Strict -or [bool]$currentManifest.signature_required
            if ($null -ne $previous) { Test-R03Package $previous $normalized $Strict | Out-Null }
            $state = [pscustomobject][ordered]@{format='noyra-r03-probe-state/v1'; installation=$script:R03Installation; generation=0; phase='ready';
                current=$current; previous=$previous; pins=@($normalized); strict=$Strict; intent=$null}
            Invoke-R03Fault 'marker.before'
            Write-R03Text $script:R03MarkerPath 'noyra-r03-probe-required/v1'
            Invoke-R03Fault 'marker.after'
            Invoke-R03Fault 'adopt.before'
            Write-R03State $state
            Invoke-R03Fault 'adopt.after'
            return $state
        }
        $state = Read-R03State
        if ($Action -ceq 'upgrade') {
            Assert-R03Segment $Transaction; Assert-R03Segment $Version
            $normalized = @(Normalize-Thumbprints $Pins)
            $effectiveStrict = $state.strict -or $Strict
            Invoke-R03Fault 'verify.before'
            $manifest = Test-R03Package $Version $normalized $effectiveStrict
            $effectiveStrict = $effectiveStrict -or [bool]$manifest.signature_required
            $digest = Get-R03ManifestHash $Version
            Invoke-R03Fault 'verify.after'
            if ($null -ne $state.intent -and $state.intent.id -ceq $Transaction) {
                if ($BaseGeneration -ne $state.intent.next_generation - 1 -or
                    $state.intent.version -cne $Version -or $state.intent.manifest_hash -cne $digest -or
                    $state.strict -ne $effectiveStrict -or (@($state.pins) -join ',') -cne ($normalized -join ',')) {
                    throw 'Same transaction identity has conflicting contents.'
                }
                return Complete-R03Recovery $state
            }
            if ($state.phase -cne 'ready') { throw 'Pending intent conflict; no policy overwrite.' }
            if ($state.generation -ne $BaseGeneration) { throw 'Stale base generation.' }
            if ($Version -ceq $state.current -and (@($state.pins) -join ',') -ceq ($normalized -join ',') -and $effectiveStrict -eq $state.strict) {
                return $state
            }
            if ($Version -ceq $state.current) { throw 'Same-version policy change requires a separate explicit operation.' }
            $state.phase='prepared'
            $state.pins=@($normalized)
            $state.strict=$effectiveStrict
            $state.intent=[pscustomobject][ordered]@{id=$Transaction; version=$Version; manifest_hash=$digest;
                strict=$effectiveStrict; previous=$state.current; next_generation=($state.generation+1)}
            Invoke-R03Fault 'prepare.before'
            Write-R03State $state
            Invoke-R03Fault 'prepare.after'
            return Complete-R03Recovery $state
        }
        if ($Action -ceq 'recover') { return Complete-R03Recovery $state }
        if ($Action -ceq 'status') { return $state }
        if ($Action -cin @('authorize-start','authorize-rollback')) {
            if ($state.phase -cne 'ready') { throw 'Protocol recovery required before start/rollback.' }
            $selected = if ($Action -ceq 'authorize-start') { $state.current } else { $state.previous }
            if ($null -eq $selected) { throw 'No rollback candidate.' }
            Test-R03Package $selected @($state.pins) $state.strict | Out-Null
            return $selected
        }
        throw 'Unsupported prototype action.'
    }
    finally { $lock.Dispose() }
}
