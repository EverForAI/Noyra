$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running equal interaction cognition audit...'
& $python -m pytest tests/test_cognition.py tests/test_interaction.py tests/test_service.py -q
if ($LASTEXITCODE -ne 0) { throw 'Interaction cognition tests failed.' }
& $python -m pytest --cov=noyra.cognition --cov=noyra.interaction --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_cognition.py tests/test_interaction.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction cognition coverage audit failed.' }
& $python -m ruff check src/noyra/cognition src/noyra/interaction src/noyra/service.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction cognition lint audit failed.' }
& $python -m ruff format --check src/noyra/cognition src/noyra/interaction src/noyra/service.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction cognition format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Interaction cognition type audit failed.' }

Write-Host 'Equal interaction cognition audit passed.'
