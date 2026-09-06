$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

Write-Host 'Checking Python and package integrity...'
$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'
& $python --version
if ($LASTEXITCODE -ne 0) { throw 'Python runtime check failed.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Package integrity check failed.' }
& $python -m pytest --cov=noyra --cov-report=term-missing
if ($LASTEXITCODE -ne 0) { throw 'Test suite failed.' }
& $python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw 'Lint audit failed.' }
& $python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { throw 'Format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Type audit failed.' }

Write-Host 'Checking Git integrity...'
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git whitespace audit failed.' }
git fsck --full
if ($LASTEXITCODE -ne 0) { throw 'Git object integrity audit failed.' }

Write-Host 'Foundation audit passed.'
