$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running causal mind-state audit...'
& $python -m pytest tests/test_mind.py -q
if ($LASTEXITCODE -ne 0) { throw 'Mind-state tests failed.' }
& $python -m pytest --cov=noyra.mind --cov-report=term-missing --cov-fail-under=85
if ($LASTEXITCODE -ne 0) { throw 'Mind-state coverage audit failed.' }
& $python -m ruff check src/noyra/mind src/noyra/core/database.py tests/test_mind.py
if ($LASTEXITCODE -ne 0) { throw 'Mind-state lint audit failed.' }
& $python -m ruff format --check src/noyra/mind src/noyra/core/database.py tests/test_mind.py
if ($LASTEXITCODE -ne 0) { throw 'Mind-state format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Mind-state type audit failed.' }

Write-Host 'Causal mind-state audit passed.'
