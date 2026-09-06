$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running fatigue and sleep audit...'
& $python -m pytest tests/test_sleep.py -q
if ($LASTEXITCODE -ne 0) { throw 'Sleep tests failed.' }
& $python -m pytest --cov=noyra.sleep --cov-report=term-missing --cov-fail-under=85 tests/test_sleep.py
if ($LASTEXITCODE -ne 0) { throw 'Sleep coverage audit failed.' }
& $python -m ruff check src/noyra/sleep src/noyra/core src/noyra/mind/memory.py src/noyra/mind/belief.py src/noyra/model/gateway.py tests/test_sleep.py
if ($LASTEXITCODE -ne 0) { throw 'Sleep lint audit failed.' }
& $python -m ruff format --check src/noyra/sleep src/noyra/core src/noyra/mind/memory.py src/noyra/mind/belief.py src/noyra/model/gateway.py tests/test_sleep.py
if ($LASTEXITCODE -ne 0) { throw 'Sleep format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Sleep type audit failed.' }

Write-Host 'Fatigue and sleep audit passed.'
