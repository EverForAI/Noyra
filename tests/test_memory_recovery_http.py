from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from noyra.cognition.types import SemanticMemoryIntegrationProposal
from noyra.core.types import content_hash
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelUsage,
    ProviderResponse,
)
from noyra.service import NoyraHTTPServer
from test_a0_project_recovery import case as case
from test_authentication_input_contract import TOKENS, call
from test_project_execution_recovery_http import http as http


def legacy(http: NoyraHTTPServer) -> Any:
    gateway = ModelGateway(
        FakeProvider(
            [
                ProviderResponse(
                    content=json.dumps({"disposition": "wait", "reason": "Old wait result"}),
                    usage=ModelUsage(1, 1),
                )
            ]
        ),
        ModelLedger(http.kernel.database),
        model="fixture",
        limits=BudgetLimits(100, 100000, 100000, 100000),
    )
    return gateway


@pytest.mark.asyncio
async def test_legacy_disposition_is_paused_operator_csrf_snapshot_bound_and_atomic(
    http: NoyraHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = legacy(http)
    await gateway.complete_structured(
        http.kernel.subject_id,
        "semantic_memory_integration:0",
        [ModelMessage(role="user", content="Synthetic fixture")],
        SemanticMemoryIntegrationProposal,
        idempotency_key="legacy",
    )
    path = "/api/admin/memory-integration-recovery"
    headers = {"Authorization": "Bearer " + TOKENS["operator"]}
    for auth in ({}, {"Authorization": "Bearer " + TOKENS["read"]}):
        assert call(http, path, headers=auth)[0] == 401
        assert call(http, path + "/discard-legacy", headers=auth, payload={})[0] == 401
    status, catalog, _ = call(http, path, headers=headers)
    assert status == 200
    item = catalog["records"][0]
    assert not item["can_discard"]
    payload = {
        "call_id": item["call_id"],
        "expected_fingerprint": item["expected_fingerprint"],
        "evidence_sha256": content_hash("review evidence"),
        "reason": "Private operator review explanation",
    }
    assert call(http, path + "/discard-legacy", headers=headers, payload=payload)[0] == 409
    assert http.operator_controls is not None
    http.operator_controls.pause(actor="operator", reason="Review legacy result")
    _, login, cookie = call(http, "/admin/session", payload={"token": TOKENS["operator"]})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(http, path + "/discard-legacy", headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    assert (
        call(
            http,
            path + "/discard-legacy",
            headers=session,
            payload={**payload, "expected_fingerprint": "0" * 64},
        )[0]
        == 409
    )
    lease = http.kernel.admission.operation("synthetic", allow_quarantine=True)
    with lease:
        assert call(http, path + "/discard-legacy", headers=session, payload=payload)[0] == 409
    # Service-level audit and receipt share the exact same transaction.
    original = http.operator_controls._audit_connection

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(http.operator_controls, "_audit_connection", broken)
    with pytest.raises(RuntimeError, match="audit failure"):
        http.operator_controls.discard_legacy_memory_result(
            item["call_id"],
            expected_fingerprint=item["expected_fingerprint"],
            evidence_sha256=payload["evidence_sha256"],
            actor="operator",
            reason=payload["reason"],
        )
    assert not call(http, path, headers=headers)[1]["records"][0]["discarded"]
    monkeypatch.setattr(http.operator_controls, "_audit_connection", original)
    for duplicate in (False, True):
        status, result, _ = call(
            http,
            path.replace("/api/", "/api/v1/") + "/discard-legacy",
            headers=session,
            payload=payload,
        )
        assert status == 200 and result["idempotent"] is duplicate
    assert payload["reason"] not in json.dumps(call(http, path, headers=headers)[1])
    assert http.kernel.lifecycle.current().state == "paused"
    with http.kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='semantic_memory_legacy_discarded'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT status FROM model_calls").fetchone()[0] == "succeeded"
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("boundary", ["quarantine", "at_rest", "migration"])
def test_legacy_memory_recovery_cannot_bypass_runtime_boundaries(
    http: NoyraHTTPServer, boundary: str
) -> None:
    from noyra.core.at_rest import AtRestError

    assert http.operator_controls is not None
    http.operator_controls.pause(actor="operator", reason="Boundary test")
    if boundary == "quarantine":
        http.kernel.admission.quarantine()
    elif boundary == "at_rest":
        http.at_rest = Mock(required=True)
        http.at_rest.require_request_ready = Mock(side_effect=AtRestError("locked"))
    else:
        with http.kernel.admission.migration_control_scope():
            http.kernel.admission.fence_for_migration(timeout=0)
    status, _, _ = call(
        http,
        "/api/admin/memory-integration-recovery/discard-legacy",
        headers={"Authorization": "Bearer " + TOKENS["operator"]},
        payload={
            "call_id": "missing",
            "expected_fingerprint": "a" * 64,
            "evidence_sha256": "b" * 64,
            "reason": "No bypass",
        },
    )
    assert status == (409 if boundary == "migration" else 503)
