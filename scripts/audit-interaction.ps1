$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running interaction and privacy projection audit...'
& $python -m pytest tests/test_interaction.py -q
if ($LASTEXITCODE -ne 0) { throw 'Interaction tests failed.' }
& $python -m pytest --cov=noyra.interaction --cov-report=term-missing --cov-fail-under=85 tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction coverage audit failed.' }
& $python -m ruff check src/noyra/interaction src/noyra/core/database.py tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction lint audit failed.' }
& $python -m ruff format --check src/noyra/interaction src/noyra/core/database.py tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Interaction format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Interaction type audit failed.' }

Write-Host 'Interaction and privacy projection audit passed.'
