$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running capability and autonomy-loop audit...'
& $python -m pytest tests/test_capability.py -q
if ($LASTEXITCODE -ne 0) { throw 'Capability tests failed.' }
& $python -m pytest --cov=noyra.capability --cov=noyra.autonomy --cov-report=term-missing --cov-fail-under=85 tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Capability coverage audit failed.' }
& $python -m ruff check src/noyra/capability src/noyra/autonomy src/noyra/core/database.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Capability lint audit failed.' }
& $python -m ruff format --check src/noyra/capability src/noyra/autonomy src/noyra/core/database.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Capability format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Capability type audit failed.' }

Write-Host 'Capability and autonomy-loop audit passed.'
