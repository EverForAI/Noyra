from __future__ import annotations

from noyra.core.types import content_hash
from noyra.service import NoyraService
from test_authentication_input_contract import call
from test_m42_p3_06_operator_controls import service as service


def test_oversized_workflow_http_is_metadata_only_and_cannot_authorize_ack(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, token, reader = service
    subject = instance.kernel.subject_id
    with instance.kernel.database.transaction() as c:
        c.executemany(
            "INSERT INTO workflow_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "http-large",
                    subject,
                    "fixture",
                    version,
                    "running",
                    "{}",
                    content_hash({}),
                    "PRIVATE_WORKFLOW_REASON",
                    "2026-11-06T00:00:00+00:00",
                )
                for version in range(1, 50_002)
            ],
        )
    path = "/api/admin/workflow-clock?workflow_id=http-large"
    for auth in ({}, {"Authorization": "Bearer " + reader}):
        assert call(instance.http, path, headers=auth)[0] == 401
    headers = {"Authorization": "Bearer " + token}
    status, result, _ = call(instance.http, path, headers=headers)
    assert status == 200 and result["status"] == "maintenance_required"
    assert not result["history_verified"] and result["expected_fingerprint"] is None
    assert result["unexplained_count"] is None and result["unexplained_versions"] is None
    assert result["capacity"]["records"] == 50_001
    assert result["maintenance"]["execution_available"]
    assert result["maintenance"]["execution_route"] == "/api/v1/admin/workflow-exports"
    assert "PRIVATE_WORKFLOW_REASON" not in str(result)
    assert call(instance.http, path.replace("/api/", "/api/v1/"), headers=headers)[1] == result
    assert (
        instance.kernel.lifecycle.current().state == "active"
        and instance.kernel.admission.accepting
    )
    assert instance.http.operator_controls is not None
    instance.http.operator_controls.pause(actor="operator", reason="Synthetic maintenance")
    with instance.kernel.database.read_transaction() as c:
        before = {
            table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "events",
                "audit_records",
                "model_calls",
                "model_attempts",
                "workflow_checkpoints",
            )
        }
    assert (
        call(
            instance.http,
            "/api/admin/workflow-clock/acknowledge",
            headers=headers,
            payload={
                "workflow_id": "http-large",
                "expected_fingerprint": "0" * 64,
                "evidence_sha256": content_hash("synthetic"),
                "reason": "Synthetic review",
            },
        )[0]
        == 409
    )
    with instance.kernel.database.read_transaction() as c:
        after = {
            table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in before
        }
    assert after == before
    assert (
        instance.kernel.lifecycle.current().state == "paused"
        and not instance.kernel.admission.accepting
    )
