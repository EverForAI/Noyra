$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running long-term memory lifecycle audit...'
& $python -m pytest tests/test_memory_lifecycle.py tests/test_mind.py tests/test_sleep.py tests/test_cognition.py tests/test_goal_governance.py -q
if ($LASTEXITCODE -ne 0) { throw 'Memory lifecycle tests failed.' }
& $python -m pytest --cov=noyra.mind.memory --cov=noyra.mind.consolidation --cov-report=term-missing --cov-fail-under=82 tests/test_memory_lifecycle.py tests/test_mind.py
if ($LASTEXITCODE -ne 0) { throw 'Memory lifecycle coverage audit failed.' }
& $python -m ruff check src tests
if ($LASTEXITCODE -ne 0) { throw 'Memory lifecycle lint audit failed.' }
& $python -m ruff format --check src tests
if ($LASTEXITCODE -ne 0) { throw 'Memory lifecycle format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Memory lifecycle type audit failed.' }

Write-Host 'Long-term memory lifecycle audit passed.'
