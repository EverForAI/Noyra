from __future__ import annotations

import pytest

from noyra.http_access_logging import http_log_fields
from noyra.service import NoyraService
from noyra.service_contract import route_contracts
from test_authentication_input_contract import call
from test_m42_p3_06_operator_controls import service as service

ROUTES = (
    ("GET", "/sleep-recovery"),
    ("POST", "/sleep-recovery/abort"),
    ("GET", "/workflow-exports"),
    ("POST", "/workflow-exports"),
    ("POST", "/workflow-exports/verify"),
    ("GET", "/workflow-increments"),
    ("POST", "/workflow-increments"),
    ("POST", "/workflow-increments/verify"),
)


@pytest.mark.parametrize("method,suffix", ROUTES)
@pytest.mark.parametrize("prefix", ("/api/admin", "/api/v1/admin"))
def test_maintenance_aliases_have_operator_contract_and_private_safe_logs(
    method: str, suffix: str, prefix: str
) -> None:
    canonical = "/api/v1/admin" + suffix
    contract = next(r for r in route_contracts() if (r.method, r.path) == (method, canonical))
    assert contract.role == "operator"
    assert {200, 401, 503} <= set(contract.effective_responses)
    assert contract.request_json is (method == "POST")
    assert http_log_fields(method, prefix + suffix + "?reason=PRIVATE", 401) == (
        method,
        canonical,
        401,
    )


def test_maintenance_boundary_statuses_match_contract_for_both_aliases(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator, readonly = service
    for prefix in ("/api/admin", "/api/v1/admin"):
        for method, suffix in ROUTES:
            payload = {} if method == "POST" else None
            path = prefix + suffix
            contract = next(
                r
                for r in route_contracts()
                if (r.method, r.path) == (method, "/api/v1/admin" + suffix)
            )
            for token in (None, readonly):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                status, _, _ = call(instance.http, path, headers=headers, payload=payload)
                assert status == 401 and status in contract.effective_responses
            headers = {"Authorization": "Bearer " + operator}
            if payload is not None:
                status, _, _ = call(
                    instance.http,
                    path,
                    payload=payload,
                    headers={**headers, "Content-Type": "text/plain"},
                )
                assert status == 415 and status in contract.effective_responses
                status, _, _ = call(instance.http, path, payload=payload, headers=headers)
                assert status == 400 and status in contract.effective_responses
    instance.http.operator_controls = None
    for method, suffix in ROUTES:
        status, _, _ = call(
            instance.http,
            "/api/v1/admin" + suffix,
            payload={} if method == "POST" else None,
            headers={"Authorization": "Bearer " + operator},
        )
        assert status == 503
