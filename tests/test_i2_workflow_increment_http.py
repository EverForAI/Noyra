from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

from noyra.autonomy.workflow import DurableWorkflowStore
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


def test_publication_http_obeys_operator_csrf_pause_and_private_response_contract(
    service: tuple[NoyraService, str, str],
    monkeypatch: Any,
) -> None:
    instance, operator, readonly = service
    # Simulated backend for protocol/auth tests; native admission is tested separately.
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: True))
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    controls = instance.http.operator_controls
    assert controls is not None
    workflow = DurableWorkflowStore(instance.kernel.database)
    workflow.checkpoint(
        instance.kernel.subject_id,
        "http-increment",
        "fixture",
        "running",
        {"private": "PRIVATE_INCREMENT_STATE"},
        reason="PRIVATE_INCREMENT_REASON",
    )
    path = "/api/v1/admin/workflow-increments"
    headers = {"Authorization": "Bearer " + operator}
    payload = {
        "workflow_id": "http-increment",
        "parent_id": "wexport_" + "0" * 64,
        "expected_version": 2,
        "request_key": "http-original-increment",
        "reason": "Synthetic export",
    }
    for auth in [{}, {"Authorization": "Bearer " + readonly}]:
        assert call(instance.http, path, headers=auth)[0] == 401
        assert call(instance.http, path, headers=auth, payload=payload)[0] == 401
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 503
    controls.pause(actor="operator", reason="Synthetic maintenance")
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        base = controls.export_workflow(
            "http-increment", 1, "http-base-key", actor="operator", reason="Synthetic export"
        )
    payload["parent_id"] = base["publication_id"]
    workflow.checkpoint(
        instance.kernel.subject_id,
        "http-increment",
        "fixture",
        "running",
        {"private": "PRIVATE_INCREMENT_SUFFIX"},
        reason="PRIVATE_INCREMENT_REASON",
    )
    _, login, cookie = call(instance.http, "/admin/session", payload={"token": operator})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(instance.http, path, headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    invalid: list[dict[str, Any]] = [
        {**payload, "expected_version": True},
        {**payload, "target": "arbitrary.sqlite3"},
        {**payload, "parent_id": 5},
    ]
    for request in invalid:
        assert call(instance.http, path, headers=session, payload=request)[0] == 400
    assert (
        call(instance.http, path, headers=headers, payload={**payload, "expected_version": 3})[0]
        == 409
    )
    with patch.object(WorkflowIncrementStore, "flush", return_value=False):
        status, result, _ = call(instance.http, path, headers=session, payload=payload)
    assert status == 200 and result["status"] == "published_durability_unknown"
    assert result["readback_verified"] and not result["durability_confirmed"]
    assert result["restore_or_dispatch_authorized"] is False
    assert "PRIVATE_INCREMENT" not in str(result)
    assert not {"manifest", "key", "state_json", "path", "reason"} & set(result)
    status, directory, _ = call(instance.http, path, headers=headers)
    assert status == 200 and len(directory["items"]) == 1
    assert directory["items"][0]["artifact_observation"] == "not_checked_by_catalog_read"
    assert call(instance.http, path + "?limit=0", headers=headers)[0] == 400
    assert call(instance.http, path + "?limit=1&limit=2", headers=headers)[0] == 400
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        accepted = call(instance.http, path, headers=headers, payload=payload)[1]
    assert accepted["status"] == "accepted" and accepted["durability_confirmed"]
    assert (
        call(instance.http, path.replace("/api/v1/", "/api/"), headers=headers)[1]["items"][0][
            "status"
        ]
        == "accepted"
    )
    instance.kernel.admission.quarantine()
    assert call(instance.http, path, headers=session, payload=payload)[0] == 503
