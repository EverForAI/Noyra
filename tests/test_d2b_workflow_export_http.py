from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.service import NoyraService
from test_authentication_input_contract import call
from test_m42_p3_06_operator_controls import service as service


def test_export_http_checks_operator_csrf_pause_and_returns_no_private_artifacts(
    service: tuple[NoyraService, str, str],
    monkeypatch: Any,
) -> None:
    instance, token, read = service
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    store = DurableWorkflowStore(instance.kernel.database)
    store.checkpoint(
        instance.kernel.subject_id,
        "http-workflow",
        "fixture",
        "running",
        {"private": "NEVER_EXPOSE_THIS_STATE"},
        reason="PRIVATE_REASON",
    )
    path = "/api/v1/admin/workflow-exports"
    headers = {"Authorization": "Bearer " + token}
    payload = {
        "workflow_id": "http-workflow",
        "expected_version": 1,
        "request_key": "http-request-key",
        "reason": "Synthetic export",
    }
    for auth in ({}, {"Authorization": "Bearer " + read}):
        assert call(instance.http, path, headers=auth)[0] == 401
        assert call(instance.http, path, headers=auth, payload=payload)[0] == 401
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 503
    assert instance.http.operator_controls is not None
    instance.http.operator_controls.pause(actor="operator", reason="Synthetic export")
    _, login, cookie = call(instance.http, "/admin/session", payload={"token": token})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(instance.http, path, headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    assert (
        call(instance.http, path, headers=session, payload={**payload, "expected_version": True})[0]
        == 400
    )
    assert (
        call(instance.http, path, headers=session, payload={**payload, "expected_version": 2})[0]
        == 409
    )
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        status, result, _ = call(instance.http, path, headers=session, payload=payload)
    assert status == 200 and result["status"] == "accepted"
    assert result["readback_verified"] and result["source_history_preserved"]
    assert "NEVER_EXPOSE_THIS_STATE" not in str(result) and "PRIVATE_REASON" not in str(result)
    assert "manifest" not in result and "key" not in result
    status, directory, _ = call(instance.http, path, headers=headers)
    assert status == 200 and len(directory["items"]) == 1
    assert directory["items"][0]["artifact_observation"] == "not_checked_by_catalog_read"
    assert call(instance.http, path.replace("/api/v1/", "/api/"), headers=headers)[1] == directory
    assert call(instance.http, path + "?limit=0", headers=headers)[0] == 400
    assert call(instance.http, path + "?limit=5&limit=6", headers=headers)[0] == 400
    instance.kernel.admission.quarantine()
    assert call(instance.http, path, headers=session, payload=payload)[0] == 503
    assert instance.kernel.lifecycle.current().state == "paused"
