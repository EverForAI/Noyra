$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running remote model-gateway audit...'
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency integrity check failed.' }
& $python -m pytest tests/test_model_gateway.py -q
if ($LASTEXITCODE -ne 0) { throw 'Model-gateway tests failed.' }
& $python -m pytest --cov=noyra.model --cov-report=term-missing --cov-fail-under=85
if ($LASTEXITCODE -ne 0) { throw 'Model-gateway coverage audit failed.' }
& $python -m ruff check src/noyra/model src/noyra/core/database.py tests/test_model_gateway.py
if ($LASTEXITCODE -ne 0) { throw 'Model-gateway lint audit failed.' }
& $python -m ruff format --check src/noyra/model src/noyra/core/database.py tests/test_model_gateway.py
if ($LASTEXITCODE -ne 0) { throw 'Model-gateway format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Model-gateway type audit failed.' }

Write-Host 'Remote model-gateway audit passed.'
