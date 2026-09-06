$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BackupRepo = (& git -C $ProjectRoot remote get-url backup).Trim()

if ($LASTEXITCODE -ne 0 -or -not $BackupRepo) {
    throw 'Git backup remote is not configured.'
}

if (-not (Test-Path -LiteralPath (Join-Path $BackupRepo 'HEAD'))) {
    throw "Git backup repository not found: $BackupRepo"
}

git -C $ProjectRoot push backup main --tags
Write-Host "Git backup updated: $BackupRepo"
