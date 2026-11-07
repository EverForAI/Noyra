from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.errors import SchemaContractError
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from noyra.service import NoyraService
from test_authentication_input_contract import call as http_call
from test_m42_p3_06_operator_controls import service as service


def call(
    server: Any,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    maintenance = payload is not None and path.startswith(
        (
            "/api/v1/admin/workflow-",
            "/api/admin/workflow-",
        )
    )
    return http_call(
        server, path, payload=payload, headers=headers, timeout=130.0 if maintenance else 5.0
    )


def test_chain_verify_http_preserves_operator_csrf_private_and_base_contracts(
    service: tuple[NoyraService, str, str],
    monkeypatch: Any,
) -> None:
    instance, operator, readonly = service
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    controls = instance.http.operator_controls
    assert controls is not None
    workflow = DurableWorkflowStore(instance.kernel.database)
    workflow.checkpoint(
        instance.kernel.subject_id,
        "http-chain",
        "fixture",
        "running",
        {"secret": "PRIVATE_CHAIN_STATE"},
        reason="PRIVATE_CHAIN_REASON",
    )
    path = "/api/v1/admin/workflow-increments/verify"
    headers = {"Authorization": "Bearer " + operator}
    payload = {"tip_id": "wdelta_" + "0" * 64}
    for auth in [{}, {"Authorization": "Bearer " + readonly}]:
        assert call(instance.http, path, headers=auth, payload=payload)[0] == 401
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 503
    controls.pause(actor="operator", reason="Synthetic evidence verification")
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 409
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        base = controls.export_workflow(
            "http-chain", 1, "chain-base-request", actor="operator", reason="Synthetic base"
        )
    workflow.checkpoint(
        instance.kernel.subject_id,
        "http-chain",
        "fixture",
        "running",
        {"secret": "PRIVATE_CHAIN_SUFFIX"},
        reason="PRIVATE_CHAIN_REASON",
    )
    with patch.object(WorkflowIncrementStore, "flush", return_value=False):
        pending = controls.export_workflow_increment(
            "http-chain",
            base["publication_id"],
            2,
            "chain-increment-request",
            actor="operator",
            reason="Synthetic increment",
        )
    payload = {"tip_id": pending["publication_id"]}
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 409
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        controls.export_workflow_increment(
            "http-chain",
            base["publication_id"],
            2,
            "chain-increment-request",
            actor="operator",
            reason="Synthetic increment",
        )
    _, login, cookie = call(instance.http, "/admin/session", payload={"token": operator})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(instance.http, path, headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    invalid: list[dict[str, Any]] = [
        {"tip_id": True},
        {**payload, "target": "restore.sqlite3"},
        {"publication_id": payload["tip_id"]},
        {},
    ]
    for request in invalid:
        assert call(instance.http, path, headers=session, payload=request)[0] == 400
    with instance.kernel.database.read_transaction() as c:
        before = {
            table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("events", "audit_records", "workflow_increment_publications")
        }
    status, result, _ = call(instance.http, path, headers=session, payload=payload)
    assert status == 200 and result["tip_id"] == pending["publication_id"]
    assert result["records"] == 2 and result["chain_nodes"] == 2
    assert result["ancestor_boundaries_verified"] == 2 and result["complete_history_verified"]
    assert result["restore_or_dispatch_authorized"] is False
    assert "PRIVATE_CHAIN" not in str(result)
    assert not {"records_json", "state_json", "manifest", "key", "path", "reason"} & set(result)
    assert (
        call(instance.http, path.replace("/api/v1/", "/api/"), headers=headers, payload=payload)[1]
        == result
    )
    base_result = call(
        instance.http, path, headers=headers, payload={"tip_id": base["publication_id"]}
    )[1]
    assert base_result["chain_nodes"] == 1 and base_result["records"] == 1
    original = call(
        instance.http,
        "/api/v1/admin/workflow-exports/verify",
        headers=headers,
        payload={"publication_id": base["publication_id"]},
    )
    assert original[0] == 200 and original[1]["records"] == 1
    with instance.kernel.database.read_transaction() as c:
        assert before == {
            table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in before
        }
    with patch.object(
        controls, "verify_workflow_chain", side_effect=SchemaContractError("fixture")
    ):
        assert call(instance.http, path, headers=session, payload=payload)[0] == 409
    instance.kernel.admission.quarantine()
    assert call(instance.http, path, headers=session, payload=payload)[0] == 503
