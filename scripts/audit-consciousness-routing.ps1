$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m pytest tests/test_consciousness_routing.py tests/test_model_gateway.py tests/test_service.py -q
& .\.venv\Scripts\python.exe -m ruff check src tests/test_consciousness_routing.py
& .\.venv\Scripts\python.exe -m mypy --strict src/noyra/model src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py
