$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$required = @(
    'docs\README.md',
    'docs\NOYRA_PROJECT_PLAN.md',
    'docs\charter.md',
    'docs\state-model.md',
    'docs\lifecycle.md',
    'docs\interaction.md',
    'docs\genesis-experience.md',
    'docs\evaluation.md',
    'docs\security-model.md',
    'docs\adr\0001-runtime-ownership.md'
)

foreach ($relative in $required) {
    $path = Join-Path $ProjectRoot $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required specification is missing: $relative"
    }
}

$allDocs = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'docs') -Recurse -File -Filter '*.md'
$text = ($allDocs | ForEach-Object { Get-Content -LiteralPath $_.FullName -Encoding UTF8 -Raw }) -join "`n"
$requiredTerms = @(
    'Noyra',
    'BehaviorLogEntry',
    'private',
    'subject_id',
    'reflective_sleep',
    'interaction_invitation',
    'human_proposal',
    'prediction_id',
    'proposal'
)
foreach ($term in $requiredTerms) {
    if ($text -notmatch [regex]::Escape($term)) {
        throw "Required specification term is missing: $term"
    }
}

foreach ($doc in $allDocs) {
    $content = Get-Content -LiteralPath $doc.FullName -Encoding UTF8
    $fences = @($content | Where-Object { $_ -match '^```' }).Count
    if (($fences % 2) -ne 0) {
        throw "Unbalanced Markdown code fence: $($doc.FullName)"
    }
}

Write-Host "Specification audit passed: $($allDocs.Count) Markdown files."
