$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running autonomous reflective sleep audit...'
& $python -m pytest tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py -q
if ($LASTEXITCODE -ne 0) { throw 'Reflective sleep tests failed.' }
& $python -m pytest --cov=noyra.cognition.reflection --cov=noyra.sleep --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Reflective sleep coverage audit failed.' }
& $python -m ruff check src/noyra/cognition src/noyra/sleep src/noyra/service.py tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Reflective sleep lint audit failed.' }
& $python -m ruff format --check src/noyra/cognition src/noyra/sleep src/noyra/service.py tests/test_reflection_cognition.py tests/test_sleep.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Reflective sleep format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Reflective sleep type audit failed.' }

Write-Host 'Autonomous reflective sleep audit passed.'
