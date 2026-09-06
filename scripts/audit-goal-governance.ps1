$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running autonomous goal governance audit...'
& $python -m pytest tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py -q
if ($LASTEXITCODE -ne 0) { throw 'Goal governance tests failed.' }
& $python -m pytest --cov=noyra.cognition.governance --cov=noyra.cognition.reflection --cov=noyra.interaction.projection --cov=noyra.service --cov-report=term-missing --cov-fail-under=85 tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Goal governance coverage audit failed.' }
& $python -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Goal governance lint audit failed.' }
& $python -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py src/noyra/core/database.py tests/test_goal_governance.py tests/test_cognition.py tests/test_reflection_cognition.py tests/test_service.py tests/test_interaction.py
if ($LASTEXITCODE -ne 0) { throw 'Goal governance format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Goal governance type audit failed.' }

Write-Host 'Autonomous goal governance audit passed.'
