$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

function Invoke-CheckedPython {
    & $python @args
    if ($LASTEXITCODE -ne 0) {
        throw "Python audit command failed with exit code $LASTEXITCODE"
    }
}

Write-Host 'Running intrinsic motivation and mission audit...'
Invoke-CheckedPython -m pytest --cov=noyra.cognition.motivation --cov=noyra.cognition.cycle --cov=noyra.cognition.self_model --cov=noyra.interaction.projection --cov=noyra.core.runtime_export --cov-report=term-missing --cov-fail-under=82 tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
Invoke-CheckedPython -m ruff check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
Invoke-CheckedPython -m ruff format --check src/noyra/cognition src/noyra/interaction/projection.py src/noyra/core/database.py src/noyra/core/runtime_export.py tests/test_motivation_development.py tests/test_cognition.py tests/test_self_model.py tests/test_interaction.py tests/test_service.py
Invoke-CheckedPython -m mypy src tests
Write-Host 'Intrinsic motivation and mission audit passed.'
