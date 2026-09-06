$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$NoyraRoot = (Resolve-Path (Join-Path $ProjectRoot '..')).Path
$CacheRoot = Join-Path $NoyraRoot '.cache'

$env:NOYRA_PROJECT_ROOT = $ProjectRoot
$env:NOYRA_DATA_DIR = Join-Path $ProjectRoot '.runtime\data'
$env:NOYRA_LOG_DIR = Join-Path $ProjectRoot '.runtime\logs'
$env:NOYRA_ARTIFACT_DIR = Join-Path $ProjectRoot '.runtime\artifacts'
$env:PIP_CACHE_DIR = Join-Path $CacheRoot 'pip'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $CacheRoot 'playwright'
$env:TEMP = Join-Path $CacheRoot 'tmp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'

@(
    $env:NOYRA_DATA_DIR,
    $env:NOYRA_LOG_DIR,
    $env:NOYRA_ARTIFACT_DIR,
    $env:PIP_CACHE_DIR,
    $env:PLAYWRIGHT_BROWSERS_PATH,
    $env:TEMP
) | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}
