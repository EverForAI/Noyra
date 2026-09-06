from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
from noyra.core.runtime import SubjectKernel
from noyra.core.types import content_hash
from noyra.mind.memory import MemoryStore
from noyra.model import BudgetLimits
from noyra.model.ledger import ModelLedger
from noyra.service import NoyraService, ServiceSettings


def _kernel(tmp_path: Path, subject_id: str) -> SubjectKernel:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"subject_id": subject_id, "test": "p3-06"}),
    )
    kernel.acquire_ownership()
    kernel.recover_after_integrity()
    kernel.orient()
    kernel.activate()
    return kernel


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[NoyraService, str, str]]:
    operator_token = "operator-" + "o" * 40
    read_token = "reader-" + "r" * 40
    instance = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "service",
            subject_id="Noyra-p306-http",
            genesis_hash=content_hash({"test": "p3-06-http"}),
            host="127.0.0.1",
            port=0,
            operator_token=SecretStr(operator_token),
            read_token=SecretStr(read_token),
            integrity_mode="off",
        )
    )
    instance.boot()
    instance.http.start()
    try:
        yield instance, operator_token, read_token
    finally:
        instance.http.close()
        instance.kernel.close()


def _request(
    service: NoyraService,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    method: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, object]]:
    _, port = service.http.address
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        method=method or ("POST" if payload is not None else "GET"),
        headers=headers,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_pause_resume_are_authorized_audited_and_idempotent(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "Noyra-p306-lifecycle")
    controls = OperatorControlService(kernel)
    try:
        first = controls.pause(actor="web-operator", reason="bounded maintenance")
        second = controls.pause(actor="web-operator", reason="duplicate request")
        resumed = controls.resume(actor="web-operator", reason="maintenance complete")

        assert first.lifecycle["state"] == "paused"
        assert first.idempotent is False
        assert second.lifecycle["version"] == first.lifecycle["version"]
        assert second.idempotent is True
        assert resumed.lifecycle["state"] == "active"
        with kernel.database.connection() as connection:
            audits = connection.execute(
                "SELECT action, actor, payload_json FROM audit_records "
                "WHERE subject_id = ? AND action IN ('operator_pause', 'operator_resume') "
                "ORDER BY occurred_at, audit_id",
                (kernel.subject_id,),
            ).fetchall()
            lifecycle_payloads = connection.execute(
                "SELECT payload_json FROM events WHERE subject_id = ? "
                "AND event_type = 'lifecycle_transition' ORDER BY occurred_at",
                (kernel.subject_id,),
            ).fetchall()
        assert [row["action"] for row in audits[-3:]] == [
            "operator_pause",
            "operator_pause",
            "operator_resume",
        ]
        assert all(row["actor"] == "web-operator" for row in audits[-3:])
        assert "bounded maintenance" in audits[-3]["payload_json"]
        assert "bounded maintenance" not in json.dumps(
            [row["payload_json"] for row in lifecycle_payloads]
        )
    finally:
        kernel.close()


def test_reset_fails_closed_until_prepared_work_is_cancelled_and_preserves_memory(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path, "Noyra-p306-reset")
    controls = OperatorControlService(kernel)
    source_event = kernel.event_store.append(
        kernel.subject_id,
        "operator_test_source",
        "test",
        {"kind": "p3-06"},
        privacy_level="private",
    )
    memory = MemoryStore(kernel.database).create(
        kernel.subject_id,
        "episodic",
        "P3-06 memory must survive transient reset",
        salience=0.5,
        confidence=0.8,
        source_event_ids=(source_event.event_id,),
    )
    action = kernel.action_ledger.prepare(
        kernel.subject_id,
        "operator_test",
        "local_test",
        "private-target",
        {"secret": "not projected"},
    )
    try:
        controls.pause(actor="web-operator", reason="prepare reset")
        with pytest.raises(
            OperatorControlConflict,
            match="reset_blocked_by_recoverable_work",
        ):
            controls.reset(actor="web-operator", reason="must fail closed")
        assert kernel.lifecycle.current().state == "paused"

        cancelled = controls.reconcile_action(
            action.action_id,
            actor="web-operator",
            reason="cancel before reset",
            outcome="cancelled",
            result={},
        )
        reset = controls.reset(actor="web-operator", reason="transient recovery")

        assert cancelled["status"] == "cancelled"
        assert reset.lifecycle["state"] == "active"
        assert reset.lifecycle["version"] >= 9
        with kernel.database.connection() as connection:
            identity_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM subject_identity WHERE subject_id = ?",
                    (kernel.subject_id,),
                ).fetchone()[0]
            )
            memory_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE memory_id = ? AND subject_id = ?",
                    (memory.memory_id, kernel.subject_id),
                ).fetchone()[0]
            )
        assert identity_count == 1
        assert memory_count == 1
    finally:
        kernel.close()


def test_unknown_action_reconciliation_is_terminal_and_never_replays(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "Noyra-p306-reconcile")
    controls = OperatorControlService(kernel)
    action = kernel.action_ledger.prepare(
        kernel.subject_id,
        "external_effect",
        "provider",
        "private-provider-target",
        {"payload": "private"},
        side_effect=True,
    )
    kernel.action_ledger.start(action.action_id)
    kernel.action_ledger.recover_interrupted(kernel.subject_id)
    try:
        reconciled = controls.reconcile_action(
            action.action_id,
            actor="web-operator",
            reason="provider confirms completion",
            outcome="succeeded",
            result={"evidence_hash": "a" * 64},
        )
        repeated = controls.reconcile_action(
            action.action_id,
            actor="web-operator",
            reason="idempotent retry",
            outcome="succeeded",
            result={"evidence_hash": "a" * 64},
        )

        assert reconciled == {
            "action_id": action.action_id,
            "status": "succeeded",
            "idempotent": False,
        }
        assert repeated["idempotent"] is True
        stored = kernel.action_ledger.recoverable(kernel.subject_id)
        assert stored == []
        with kernel.database.connection() as connection:
            revisions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_revisions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()[0]
            )
            behavior = connection.execute(
                "SELECT public_target, public_explanation FROM behavior_log_revisions "
                "WHERE action_id = ? ORDER BY revision_number DESC LIMIT 1",
                (action.action_id,),
            ).fetchone()
        assert revisions == 4
        assert behavior["public_target"] == "[private]"
        assert "provider confirms completion" not in behavior["public_explanation"]
    finally:
        kernel.close()


def test_http_lifecycle_requires_operator_role_without_mutating_state(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator_token, read_token = service

    status, body = _request(
        instance,
        "/api/admin/lifecycle/pause",
        token=read_token,
        payload={"reason": "read token must not mutate"},
    )
    assert status == 401
    assert body == {"error": "unauthorized"}
    assert instance.kernel.lifecycle.current().state == "active"

    status, paused = _request(
        instance,
        "/api/admin/lifecycle/pause",
        token=operator_token,
        payload={"reason": "operator maintenance"},
    )
    assert status == 200
    assert paused["lifecycle"]["state"] == "paused"  # type: ignore[index]

    status, resumed = _request(
        instance,
        "/api/admin/lifecycle/resume",
        token=operator_token,
        payload={"reason": "operator maintenance complete"},
    )
    assert status == 200
    assert resumed["lifecycle"]["state"] == "active"  # type: ignore[index]


def test_http_reconcile_prepared_action_then_reset_without_data_wipe(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator_token, _read_token = service
    action = instance.kernel.action_ledger.prepare(
        instance.kernel.subject_id,
        "operator_test",
        "local_test",
        "private-target",
        {"private": "payload"},
    )
    assert (
        _request(
            instance,
            "/api/admin/lifecycle/pause",
            token=operator_token,
            payload={"reason": "prepare reset"},
        )[0]
        == 200
    )

    status, blocked = _request(
        instance,
        "/api/admin/lifecycle/reset",
        token=operator_token,
        payload={"reason": "must block"},
    )
    assert status == 409
    assert blocked == {"error": "reset_blocked_by_recoverable_work"}

    status, reconciled = _request(
        instance,
        f"/api/admin/actions/{action.action_id}/reconcile",
        token=operator_token,
        payload={"reason": "cancel prepared work", "outcome": "cancelled"},
    )
    assert status == 200
    assert reconciled["status"] == "cancelled"

    status, reset = _request(
        instance,
        "/api/admin/lifecycle/reset",
        token=operator_token,
        payload={"reason": "transient reset"},
    )
    assert status == 200
    assert reset["lifecycle"]["state"] == "active"  # type: ignore[index]
    assert instance.kernel.identity_store.load(instance.kernel.subject_id).subject_id == (
        instance.kernel.subject_id
    )


def test_operator_route_errors_inventory_and_model_call_reconciliation(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator_token, read_token = service

    assert _request(instance, "/api/admin/health", token=read_token) == (
        401,
        {"error": "unauthorized"},
    )
    status, inventory = _request(
        instance,
        "/api/admin/recoverable-work?limit=10",
        token=operator_token,
    )
    assert status == 200
    assert inventory["truncated"] is False
    assert inventory["counts"] == {
        "actions": 0,
        "model_calls": 0,
        "model_attempts": 0,
        "deliveries": 0,
    }
    status, lifecycle = _request(instance, "/api/admin/lifecycle", token=operator_token)
    assert status == 200
    assert lifecycle["lifecycle"]["state"] == "active"  # type: ignore[index]

    assert _request(
        instance,
        "/api/admin/lifecycle/pause",
        token=operator_token,
        payload={"reason": "bounded", "unexpected": True},
    ) == (400, {"error": "invalid_lifecycle_control"})
    assert _request(
        instance,
        "/api/admin/lifecycle/pause",
        token=operator_token,
        payload={"reason": "bounded"},
        content_type="text/plain",
    ) == (415, {"error": "json_required"})

    call, _created = ModelLedger(instance.kernel.database).prepare_call(
        instance.kernel.subject_id,
        "test-provider",
        "test-model",
        "operator reconciliation",
        content_hash({"request": "private"}),
        "p3-06-model-call",
    )
    model_path = f"/api/admin/model-calls/{call.call_id}/reconcile"
    assert _request(
        instance,
        model_path,
        token=read_token,
        payload={"reason": "unauthorized", "outcome": "failed"},
    ) == (401, {"error": "unauthorized"})
    status, reconciled = _request(
        instance,
        model_path,
        token=operator_token,
        payload={"reason": "cancel before send", "outcome": "failed"},
    )
    assert status == 200
    assert reconciled == {"call_id": call.call_id, "status": "failed", "idempotent": False}
    assert (
        _request(
            instance,
            model_path,
            token=operator_token,
            payload={"reason": "repeat", "status": "failed"},
        )[1]["idempotent"]
        is True
    )
    assert _request(
        instance,
        "/api/admin/model-calls/missing/reconcile",
        token=operator_token,
        payload={"reason": "missing", "outcome": "failed"},
    ) == (404, {"error": "model_call_not_found"})
    assert _request(
        instance,
        "/api/admin/model-calls/missing/reconcile",
        token=operator_token,
        payload={"reason": "invalid", "outcome": "cancelled"},
    ) == (400, {"error": "invalid_reconciliation_outcome"})

    retry_call, _created = ModelLedger(instance.kernel.database).prepare_call(
        instance.kernel.subject_id,
        "test-provider",
        "test-model",
        "operator retry authorization",
        content_hash({"request": "ambiguous"}),
        "p3-06-model-call-retry",
    )
    retry_ledger = ModelLedger(instance.kernel.database)
    retry_attempt = retry_ledger.authorize_attempt(
        retry_call.call_id,
        BudgetLimits(10, 10_000, 10_000, 1_000_000),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_microusd=1,
    )
    retry_ledger.start_attempt(retry_attempt.attempt_id)
    retry_ledger.finish_attempt(
        retry_attempt.attempt_id,
        "unknown",
        usage=None,
        cost_microusd=None,
        error_code="provider_outcome_unknown",
    )
    retry_ledger.finish_call(
        retry_call.call_id,
        "unknown",
        error_code="provider_outcome_unknown",
    )
    retry_path = f"/api/admin/model-calls/{retry_call.call_id}/reconcile"
    status, authorized = _request(
        instance,
        retry_path,
        token=operator_token,
        payload={"reason": "provider confirmed replay is acceptable", "outcome": "retry"},
    )
    assert status == 200
    assert authorized == {
        "call_id": retry_call.call_id,
        "status": "prepared",
        "idempotent": False,
    }
    assert _request(
        instance,
        retry_path,
        token=operator_token,
        payload={"reason": "repeat authorization", "outcome": "retry"},
    ) == (
        200,
        {"call_id": retry_call.call_id, "status": "prepared", "idempotent": True},
    )

    assert _request(
        instance,
        "/api/admin/actions/missing/reconcile",
        token=operator_token,
        payload={"reason": "missing", "outcome": "failed"},
    ) == (404, {"error": "action_not_found"})
    assert _request(
        instance,
        "/api/admin/actions/missing/reconcile",
        token=operator_token,
        payload={"reason": "invalid", "outcome": "retry"},
    ) == (400, {"error": "invalid_reconciliation_outcome"})

    controls = instance.http.operator_controls
    instance.http.operator_controls = None
    try:
        assert _request(instance, "/api/admin/health", token=operator_token) == (
            503,
            {"error": "operator_controls_unavailable"},
        )
        assert _request(
            instance,
            "/api/admin/recoverable-work",
            token=operator_token,
        ) == (503, {"error": "operator_controls_unavailable"})
    finally:
        instance.http.operator_controls = controls


def test_health_projection_exposes_required_domains_without_private_content(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator_token, read_token = service
    secret = "P3-06-PRIVATE-OPERATOR-REASON"
    controls = instance.http.operator_controls
    assert controls is not None
    controls.pause(actor="web-operator", reason=secret)

    status, health = _request(instance, "/api/admin/health", token=operator_token)
    assert status == 200
    assert {
        "integrity",
        "archive_key",
        "migration",
        "wal",
        "storage",
        "export",
        "at_rest",
        "work",
    } <= set(health)
    encoded = json.dumps(health)
    assert secret not in encoded
    assert str(instance.settings.data_dir) not in encoded
    assert "payload_json" not in encoded

    status, diagnostics = _request(instance, "/api/diagnostics", token=read_token)
    assert status == 200
    assert diagnostics["operator_health"]["migration"]["state"] == "ok"  # type: ignore[index]
    assert diagnostics["wal"]["journal_mode"] == "wal"  # type: ignore[index]


def test_health_projection_reports_schema_failure_without_exposing_sql(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path, "Noyra-p306-health-failure")
    controls = OperatorControlService(kernel)
    try:
        with kernel.database.transaction() as connection:
            connection.execute("UPDATE schema_meta SET value = '40' WHERE key = 'schema_version'")
        health = controls.health()
        assert health["status"] == "degraded"
        assert health["migration"] == {
            "state": "degraded",
            "schema_version": 40,
            "expected_schema_version": CURRENT_SCHEMA_VERSION,
            "rollback_backups": 0,
            "reason": "schema_version_mismatch",
        }
        assert "UPDATE schema_meta" not in json.dumps(health)
    finally:
        kernel.close()
