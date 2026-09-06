$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running epistemic review and runtime export audit...'
& $python -m pytest tests/test_epistemic_review.py tests/test_service.py tests/test_world.py tests/test_cognition.py -q
if ($LASTEXITCODE -ne 0) { throw 'Epistemic review tests failed.' }
& $python -m pytest --cov=noyra.cognition.epistemic --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_epistemic_review.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Epistemic review coverage audit failed.' }
& $python -m ruff check src tests
if ($LASTEXITCODE -ne 0) { throw 'Epistemic review lint audit failed.' }
& $python -m ruff format --check src tests
if ($LASTEXITCODE -ne 0) { throw 'Epistemic review format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Epistemic review type audit failed.' }

Write-Host 'Epistemic review and runtime export audit passed.'
