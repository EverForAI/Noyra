$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running world-observation and genesis audit...'
& $python -m pytest tests/test_world.py -q
if ($LASTEXITCODE -ne 0) { throw 'World-observation tests failed.' }
& $python -m pytest --cov=noyra.world --cov-report=term-missing --cov-fail-under=85 tests/test_world.py
if ($LASTEXITCODE -ne 0) { throw 'World-observation coverage audit failed.' }
& $python -m ruff check src/noyra/world src/noyra/core/database.py src/noyra/mind/causal.py tests/test_world.py
if ($LASTEXITCODE -ne 0) { throw 'World-observation lint audit failed.' }
& $python -m ruff format --check src/noyra/world src/noyra/core/database.py src/noyra/mind/causal.py tests/test_world.py
if ($LASTEXITCODE -ne 0) { throw 'World-observation format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'World-observation type audit failed.' }

Write-Host 'World-observation and genesis audit passed.'
