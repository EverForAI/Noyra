$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

Write-Host 'Running operational self-model audit...'
& $python -m pytest --cov=noyra.cognition.self_model --cov=noyra.cognition.cycle --cov=noyra.interaction.projection --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_self_model.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
& $python -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_self_model.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
& $python -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_self_model.py tests/test_cognition.py tests/test_interaction.py tests/test_service.py
& $python -m mypy src tests
Write-Host 'Operational self-model audit passed.'
