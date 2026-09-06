#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pytest tests/test_consciousness_routing.py tests/test_model_gateway.py tests/test_service.py -q
python3 -m ruff check src tests/test_consciousness_routing.py
python3 -m mypy --strict src/noyra/model src/noyra/cognition src/noyra/interaction/projection.py src/noyra/service.py
