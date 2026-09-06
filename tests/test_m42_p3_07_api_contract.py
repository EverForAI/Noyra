from __future__ import annotations

import re
from pathlib import Path

from noyra.service import NoyraHTTPServer
from noyra.service_contract import route_contracts

ROOT = Path(__file__).parents[1]
OPENAPI = ROOT / "docs" / "api" / "openapi.yaml"


def _openapi_method_blocks() -> dict[tuple[str, str], str]:
    """Extract operation blocks without adding a YAML parser dependency.

    This deliberately validates only the inventory-level fields used by the
    route contract; it is not a replacement for a full OpenAPI schema parser.
    """
    blocks: dict[tuple[str, str], str] = {}
    in_paths = False
    current: str | None = None
    method: str | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None and method is not None:
            blocks[(current, method)] = "\n".join(body)

    for line in OPENAPI.read_text(encoding="utf-8").splitlines():
        if line == "paths:":
            in_paths = True
            continue
        if in_paths and line == "components:":
            break
        if not in_paths:
            continue
        path_match = re.fullmatch(r"  (/[^:]+):", line)
        if path_match:
            flush()
            current = path_match.group(1)
            method = None
            body = []
            continue
        method_match = re.fullmatch(r"    (get|post):", line)
        if method_match and current is not None:
            flush()
            method = method_match.group(1).upper()
            body = []
            continue
        if method is not None:
            body.append(line)
    flush()
    return blocks


def _openapi_operations() -> dict[str, set[str]]:
    operations: dict[str, set[str]] = {}
    for path, method in _openapi_method_blocks():
        operations.setdefault(path, set()).add(method)
    return operations


def _runtime_literal_paths() -> set[str]:
    source = (ROOT / "src" / "noyra" / "service.py").read_text(encoding="utf-8")
    paths = set(
        re.findall(
            r'(?:parsed\.path|self\.path)\s*==\s*"(/(?:api/[^\"]+|health(?:/(?:live|ready))?))"',
            source,
        )
    )
    return {path if path == "/health" else path.replace("/api/", "/api/v1/", 1) for path in paths}


def _runtime_prefixes() -> set[str]:
    source = (ROOT / "src" / "noyra" / "service.py").read_text(encoding="utf-8")
    prefixes = set(
        re.findall(
            r'(?:parsed\.path|self\.path)\.startswith\(\s*"(/api[^\"]*)"',
            source,
            flags=re.DOTALL,
        )
    )
    return {
        prefix.replace("/api/", "/api/v1/", 1)
        for prefix in prefixes
        if prefix not in {"/api/", "/api/v1/"}
    }


def _matches_template(path: str, template: str) -> bool:
    expression = re.sub(r"\{[^/]+\}", r"[^/]+", template)
    return re.fullmatch(expression, path) is not None


def test_every_runtime_route_is_in_the_versioned_openapi_inventory() -> None:
    contracts = route_contracts()
    contract_operations = {(route.path, route.method) for route in contracts}
    openapi_operations = _openapi_operations()
    openapi_blocks = _openapi_method_blocks()
    assert "/health" in openapi_operations
    assert set(openapi_operations) == {route.path for route in contracts}
    assert {
        (path, method) for path, methods in openapi_operations.items() for method in methods
    } == contract_operations
    for route in contracts:
        assert route.path in openapi_operations, route.path
        assert route.method in openapi_operations[route.path], (route.path, route.method)
        method_block = openapi_blocks[(route.path, route.method)]
        if route.role != "none":
            assert "security:" in method_block, (route.path, route.method)
        assert ("requestBody:" in method_block) is route.request_json, (
            route.path,
            route.method,
        )
        for status in route.responses:
            # The route inventory is the authoritative status matrix.  The
            # OpenAPI text must name every non-success response explicitly.
            assert f"'{status}':" in method_block or f'"{status}":' in method_block, (
                route.path,
                route.method,
                status,
            )
    assert len(contract_operations) == len(contracts)

    for runtime_path in _runtime_literal_paths():
        assert any(
            runtime_path == route.path or _matches_template(runtime_path, route.path)
            for route in contracts
        ), runtime_path


def test_runtime_dynamic_route_prefixes_are_in_the_contract_inventory() -> None:
    contracts = route_contracts()
    for prefix in _runtime_prefixes():
        assert any(
            re.sub(r"\{[^/]+\}", "segment", route.path).startswith(prefix.rstrip("/"))
            for route in contracts
        ), prefix


def test_route_contract_has_no_duplicate_or_unversioned_entries() -> None:
    contracts = route_contracts()
    keys = [(route.method, route.path) for route in contracts]
    assert len(keys) == len(set(keys))
    assert all(
        route.path.startswith("/health") or route.path.startswith("/api/v1/") for route in contracts
    )
    assert all(route.role in {"none", "read", "operator", "export"} for route in contracts)


def test_shared_http_boundary_errors_are_explicitly_contractual() -> None:
    contracts = route_contracts()
    openapi = OPENAPI.read_text(encoding="utf-8")
    assert "x-global-error-responses:" in openapi
    for code in ("'429':", "'503':", "'411':", "'413':"):
        assert code in openapi
    for route in contracts:
        if route.path.startswith("/api/v1/"):
            assert {429, 503} <= set(route.effective_responses)
        if route.method == "POST" and route.request_json:
            assert {411, 413} <= set(route.effective_responses)


def test_runtime_server_exposes_the_same_route_inventory() -> None:
    assert NoyraHTTPServer.api_route_contracts() == route_contracts()
