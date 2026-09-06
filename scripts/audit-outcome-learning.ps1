$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running outcome evaluation and strategy learning audit...'
& $python -m pytest tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py -q
if ($LASTEXITCODE -ne 0) { throw 'Outcome learning tests failed.' }
& $python -m pytest --cov=noyra.learning --cov-report=term-missing --cov-fail-under=85 tests/test_outcome_learning.py
if ($LASTEXITCODE -ne 0) { throw 'Outcome learning coverage audit failed.' }
& $python -m ruff check src/noyra/learning src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Outcome learning lint audit failed.' }
& $python -m ruff format --check src/noyra/learning src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_outcome_learning.py tests/test_cognition.py tests/test_service.py
if ($LASTEXITCODE -ne 0) { throw 'Outcome learning format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Outcome learning type audit failed.' }

Write-Host 'Outcome evaluation and strategy learning audit passed.'
