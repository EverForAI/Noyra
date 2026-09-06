$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running autonomous research audit...'
& $python -m pytest tests/test_research.py tests/test_cognition.py tests/test_service.py -q
if ($LASTEXITCODE -ne 0) { throw 'Autonomous research tests failed.' }
& $python -m pytest --cov=noyra.cognition.research --cov=noyra.research --cov-report=term-missing --cov-fail-under=80 tests/test_research.py tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Autonomous research coverage audit failed.' }
& $python -m ruff check src/noyra/research src/noyra/cognition/research.py src/noyra/service.py src/noyra/core/database.py tests/test_research.py tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Autonomous research lint audit failed.' }
& $python -m ruff format --check src/noyra/research src/noyra/cognition/research.py src/noyra/service.py src/noyra/core/database.py tests/test_research.py tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Autonomous research format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Autonomous research type audit failed.' }

Write-Host 'Autonomous research audit passed.'
