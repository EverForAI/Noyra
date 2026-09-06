$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running autonomous cognition audit...'
& $python -m pytest tests/test_cognition.py tests/test_service.py tests/test_capability.py -q
if ($LASTEXITCODE -ne 0) { throw 'Cognition tests failed.' }
& $python -m pytest --cov=noyra.cognition --cov=noyra.service --cov=noyra.capability.tools --cov-report=term-missing --cov-fail-under=85 tests/test_cognition.py tests/test_service.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Cognition coverage audit failed.' }
& $python -m ruff check src/noyra/cognition src/noyra/service.py src/noyra/capability tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Cognition lint audit failed.' }
& $python -m ruff format --check src/noyra/cognition src/noyra/service.py src/noyra/capability tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Cognition format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Cognition type audit failed.' }

Write-Host 'Autonomous cognition audit passed.'
