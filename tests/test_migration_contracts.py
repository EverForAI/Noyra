from __future__ import annotations

import re
from pathlib import Path

from noyra.service_contract import route_contracts

ROOT = Path(__file__).parents[1]
OPENAPI = ROOT / "docs" / "api" / "openapi.yaml"

MIGRATION_OPERATIONS = {
    ("GET", "/api/v1/admin/migration/policy"),
    ("PUT", "/api/v1/admin/migration/policy"),
    ("GET", "/api/v1/admin/migration/targets"),
    ("POST", "/api/v1/admin/migration/targets"),
    ("POST", "/api/v1/admin/migration/targets/{targetId}/revoke"),
    ("POST", "/api/v1/admin/migration/targets/{targetId}/challenge"),
    ("POST", "/api/v1/admin/migration/targets/{targetId}/attest"),
    ("GET", "/api/v1/admin/migration/candidates"),
    ("GET", "/api/v1/admin/migration/proposals"),
    ("GET", "/api/v1/admin/migration/proposals/{proposalId}"),
    ("POST", "/api/v1/admin/migration/proposals/{proposalId}/approve"),
    ("POST", "/api/v1/admin/migration/proposals/{proposalId}/reject"),
    ("POST", "/api/v1/admin/migration/tasks/{taskId}/cancel"),
    ("GET", "/api/v1/admin/migration/tasks"),
    ("GET", "/api/v1/admin/migration/tasks/{taskId}"),
    ("POST", "/api/v1/admin/migration/tasks/{taskId}/cutover"),
    ("POST", "/api/v1/admin/migration/tasks/{taskId}/rollback"),
    ("POST", "/api/v1/admin/migration/recovery"),
}


def _openapi_operations() -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    current: str | None = None
    for line in OPENAPI.read_text(encoding="utf-8").splitlines():
        path = re.fullmatch(r"  (/[^:]+):", line)
        if path:
            current = path.group(1)
            continue
        method = re.fullmatch(r"    (get|post|put|patch|delete):", line)
        if method and current:
            operations.add((method.group(1).upper(), current))
    return operations


def test_migration_route_contracts_are_operator_protected() -> None:
    contracts = {(route.method, route.path): route for route in route_contracts()}
    assert set(contracts) >= MIGRATION_OPERATIONS
    for operation in MIGRATION_OPERATIONS:
        route = contracts[operation]
        assert route.role == "operator"
        if route.method in {"POST", "PUT"}:
            assert route.request_json
        assert 401 in route.responses


def test_migration_operations_are_in_openapi_inventory() -> None:
    assert _openapi_operations() >= MIGRATION_OPERATIONS
