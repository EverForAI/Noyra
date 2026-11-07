from __future__ import annotations

from typing import Any

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.types import canonical_json, content_hash
from noyra.service import NoyraHTTPServer
from test_a0_project_recovery import case as case
from test_authentication_input_contract import TOKENS, call
from test_project_execution_recovery_http import http as http


def seed(http: NoyraHTTPServer) -> list[dict[str, Any]]:
    with http.kernel.database.transaction() as c:
        for version, hour in enumerate([12, 10, 11], 1):
            state = {"fixture": version}
            c.execute(
                "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-clock",
                    http.kernel.subject_id,
                    "fixture",
                    version,
                    "running",
                    canonical_json(state),
                    content_hash(state),
                    "fixture",
                    f"2026-11-04T{hour}:00:00+00:00",
                ),
            )
        return [dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")]


def test_clock_recovery_is_operator_csrf_snapshot_bound_and_never_resumes(
    http: NoyraHTTPServer,
) -> None:
    before = seed(http)
    path = "/api/admin/workflow-clock"
    headers = {"Authorization": "Bearer " + TOKENS["operator"]}
    for auth in ({}, {"Authorization": "Bearer " + TOKENS["read"]}):
        assert call(http, path + "?workflow_id=legacy-clock", headers=auth)[0] == 401
        assert call(http, path + "/acknowledge", payload={}, headers=auth)[0] == 401
    status, catalog, _ = call(http, path + "?workflow_id=legacy-clock", headers=headers)
    assert status == 200 and catalog["unexplained_versions"] == [2, 3]
    payload = {
        "workflow_id": "legacy-clock",
        "expected_fingerprint": catalog["expected_fingerprint"],
        "evidence_sha256": content_hash("independent synthetic evidence"),
        "reason": "Clock verified independently",
    }
    assert call(http, path + "/acknowledge", headers=headers, payload=payload)[0] == 409
    assert http.operator_controls is not None
    http.operator_controls.pause(actor="operator", reason="Clock maintenance")
    http.kernel.admission.quarantine()
    _, login, cookie = call(http, "/admin/session", payload={"token": TOKENS["operator"]})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(http, path + "/acknowledge", headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    assert (
        call(
            http,
            path + "/acknowledge",
            headers=session,
            payload={**payload, "expected_fingerprint": "0" * 64},
        )[0]
        == 409
    )
    status, result, _ = call(
        http, path.replace("/api/", "/api/v1/") + "/acknowledge", headers=session, payload=payload
    )
    assert status == 200 and result["acknowledged_versions"] == [2, 3]
    assert call(http, path + "/acknowledge", headers=session, payload=payload)[1]["idempotent"]
    assert http.kernel.admission.quarantined and http.kernel.lifecycle.current().state == "paused"
    assert DurableWorkflowStore(http.kernel.database).verify_integrity(http.kernel.subject_id) == 3
    with http.kernel.database.read_transaction() as c:
        assert [dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")] == before
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='workflow_clock_acknowledged'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_runtime_summary_prefers_actual_service_watchdog_without_mutating_cached_snapshot(
    http: NoyraHTTPServer,
) -> None:
    headers = {"Authorization": "Bearer " + TOKENS["operator"]}
    http.integrity_watchdog_health = lambda: {
        "status": "backoff",
        "persisted": False,
        "consecutive_failures": 3,
    }
    status, summary, _ = call(http, "/api/admin/runtime-summary", headers=headers)
    assert status == 200 and summary["integrity_watchdog"]["persisted"] is False
    assert summary["integrity_watchdog"]["consecutive_failures"] == 3
    http.integrity_watchdog_health = None
    status, summary, _ = call(http, "/api/admin/runtime-summary", headers=headers)
    assert status == 200 and summary["integrity_watchdog"] == {"status": "not_triggered"}
