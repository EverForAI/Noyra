$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running autonomous action deliberation audit...'
& $python -m pytest tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py tests/test_service.py -q
if ($LASTEXITCODE -ne 0) { throw 'Action deliberation tests failed.' }
& $python -m pytest --cov=noyra.cognition.deliberation --cov-report=term-missing --cov-fail-under=80 tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Action deliberation coverage audit failed.' }
& $python -m ruff check src/noyra/cognition src/noyra/capability/tools.py src/noyra/core/database.py tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Action deliberation lint audit failed.' }
& $python -m ruff format --check src/noyra/cognition src/noyra/capability/tools.py src/noyra/core/database.py tests/test_action_deliberation.py tests/test_cognition.py tests/test_capability.py
if ($LASTEXITCODE -ne 0) { throw 'Action deliberation format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Action deliberation type audit failed.' }

Write-Host 'Autonomous action deliberation audit passed.'
