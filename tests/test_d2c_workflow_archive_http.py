from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.errors import SchemaContractError
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.service import NoyraService
from test_authentication_input_contract import call
from test_m42_p3_06_operator_controls import service as service


def test_archive_verification_http_preserves_auth_csrf_pause_and_private_boundary(
    service: tuple[NoyraService, str, str],
    monkeypatch: Any,
) -> None:
    instance, token, read = service
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    DurableWorkflowStore(instance.kernel.database).checkpoint(
        instance.kernel.subject_id,
        "cold-workflow",
        "fixture",
        "running",
        {"secret": "PRIVATE_COLD_STATE"},
        reason="PRIVATE_COLD_REASON",
    )
    controls = instance.http.operator_controls
    assert controls is not None
    path = "/api/v1/admin/workflow-exports/verify"
    headers = {"Authorization": "Bearer " + token}
    payload = {"publication_id": "wexport_" + "a" * 64}
    for auth in [{}, {"Authorization": "Bearer " + read}]:
        assert call(instance.http, path, headers=auth, payload=payload)[0] == 401
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 503
    controls.pause(actor="operator", reason="Synthetic verification")
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 409
    with patch.object(WorkflowArtifactStore, "flush", return_value=False):
        saved = controls.export_workflow(
            "cold-workflow", 1, "http-cold-export", actor="operator", reason="Synthetic export"
        )
    payload = {"publication_id": saved["publication_id"]}
    assert call(instance.http, path, headers=headers, payload=payload)[0] == 409
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        saved = controls.export_workflow(
            "cold-workflow", 1, "http-cold-export", actor="operator", reason="Synthetic export"
        )
    _, login, cookie = call(instance.http, "/admin/session", payload={"token": token})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(instance.http, path, headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    invalid_requests: list[dict[str, Any]] = [
        {"publication_id": True},
        {**payload, "target": "arbitrary.sqlite3"},
        {},
    ]
    for invalid in invalid_requests:
        assert call(instance.http, path, headers=session, payload=invalid)[0] == 400
    with instance.kernel.database.read_transaction() as c:
        before = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    status, result, _ = call(instance.http, path, headers=session, payload=payload)
    assert status == 200 and result["isolated_reconstruction_verified"]
    assert result["complete_history_verified"] and result["records"] == 1
    assert result["restore_or_dispatch_authorized"] is False
    assert "PRIVATE_COLD" not in str(result)
    assert not {"state_json", "manifest", "reason", "key", "path"} & set(result)
    assert (
        call(instance.http, path.replace("/api/v1/", "/api/"), headers=headers, payload=payload)[1]
        == result
    )
    with instance.kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    with patch.object(
        controls, "verify_workflow_export", side_effect=SchemaContractError("fixture")
    ):
        assert call(instance.http, path, headers=session, payload=payload)[0] == 409
    instance.kernel.admission.quarantine()
    assert call(instance.http, path, headers=session, payload=payload)[0] == 503
