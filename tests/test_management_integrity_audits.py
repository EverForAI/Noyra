from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from noyra.core.errors import IntegrityError
from noyra.core.integrity import IntegrityRegistry, _verify_management_audit_provenance
from noyra.core.operator_controls import OperatorControlService
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.retention import RetentionManager
from noyra.core.types import canonical_json
from noyra.interaction import InboundEnvelope, PublicPostInput, TransportInput
from noyra.model.errors import ProviderCallError
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.research.search import SearchExecutor, SearchOutcomeUnknownError
from noyra.service import NoyraService
from test_m42_p1_02_integrity_runtime import _active_kernel
from test_m42_p3_06_operator_controls import _put_kernel_to_deep_sleep
from test_m42_p3_06_operator_controls import _request as management_request
from test_m42_p3_06_operator_controls import service as management_service  # noqa: F401


def test_pause_resume_reset_and_reconciliation_keep_core_integrity(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-operator-audit")
    try:
        action = kernel.action_ledger.prepare(kernel.subject_id, "observe", "test", "target", {})
        controls = OperatorControlService(kernel)
        controls.reconcile_action(
            action.action_id, outcome="cancelled", result={}, actor="operator", reason="cancel"
        )
        controls.pause(actor="operator", reason="maintenance")
        controls.reset(actor="operator", reason="reset transient state")
        controls.resume(actor="operator", reason="complete")
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert report.status == "ok", report.to_dict()
        with kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_audit_record_update")
            row = connection.execute(
                "SELECT audit_id,payload_json FROM audit_records WHERE action='operator_pause'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["reason_hash"] = "0" * 64
            connection.execute(
                "UPDATE audit_records SET payload_json=? WHERE audit_id=?",
                (canonical_json(payload), row["audit_id"]),
            )
        damaged = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert damaged.p0 == ("core.actions:integrity_error",)
    finally:
        kernel.close()


def test_operator_wake_keeps_core_actions_integrity(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-operator-wake-audit")
    try:
        _put_kernel_to_deep_sleep(kernel)
        OperatorControlService(kernel).wake(
            actor="operator", reason="wake for controlled verification"
        )
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert report.status == "ok", report.to_dict()
    finally:
        kernel.close()


def test_operations_integrity_checks_cover_provider_health_and_retention(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-operations-integrity")
    try:
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("operations.provider_health", "operations.retention"),
        )
        assert report.status == "ok", report.to_dict()
    finally:
        kernel.close()


def test_retention_integrity_recomputes_provenance_hash(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-retention-integrity-hash")
    try:
        RetentionManager(kernel.database).run_batch(kernel.subject_id)
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE retention_runs SET state_hash=? WHERE subject_id=?",
                ("0" * 64, kernel.subject_id),
            )
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("operations.retention",),
        )
        assert report.status == "corrupt", report.to_dict()
    finally:
        kernel.close()


def test_provider_health_integrity_requires_state_for_recorded_buckets(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-provider-health-state-audit")
    try:
        ProviderHealthStore(kernel.database).record_attempt(
            kernel.subject_id, "model", "provider-a", "attempt-1", False, 100, "timeout"
        )
        with kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM provider_health_state WHERE subject_id=? AND provider_id=?",
                (kernel.subject_id, "provider-a"),
            )
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("operations.provider_health",),
        )
        assert report.status == "corrupt", report.to_dict()
    finally:
        kernel.close()


@pytest.mark.usefixtures("management_service")
@pytest.mark.parametrize("damage", ["none", "role", "role_type", "duplicate_keys"])
def test_admin_audits_accept_the_service_json_format_and_validate_roles(
    request: pytest.FixtureRequest, tmp_path: Path, damage: str
) -> None:
    instance: NoyraService = request.getfixturevalue("management_service")[0]
    instance.http.audit_admin_event(
        "admin_login_failed", "web-anonymous", {"path": "/admin/session"}
    )
    instance.http.audit_admin_event("admin_login_succeeded", "web-operator", {"role": "operator"})
    instance.http.audit_admin_event("admin_logout", "web-operator", {"role": "operator"})
    instance.http.audit_admin_event("admin_break_glass_rejected_session", "web-break_glass", {})
    if damage != "none":
        with instance.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_audit_record_update")
            connection.execute(
                "UPDATE audit_records SET payload_json=? WHERE action='admin_login_succeeded'",
                (
                    {
                        "role": json.dumps({"role": "unrecognized"}, sort_keys=True),
                        "role_type": json.dumps({"role": []}, sort_keys=True),
                        "duplicate_keys": '{"role": "admin", "role": "operator"}',
                    }[damage],
                ),
            )
    report = IntegrityRegistry().run(
        instance.kernel.database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == ("ok" if damage == "none" else "corrupt"), report.to_dict()


@pytest.mark.usefixtures("management_service")
@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("model_resource_discovery_succeeded", {"model_count": 2}),
        ("model_resource_discovery_failed", {"error": "provider_auth_failed"}),
        ("model_resource_discovery_rejected", {"error": "invalid_model_discovery"}),
        ("model_resource_draft_test_succeeded", {"model": "测试模型"}),
        ("model_resource_draft_test_failed", {"error": "provider_http_429"}),
        ("model_resource_draft_test_rejected", {"error": "invalid_model_resource_test"}),
    ],
)
@pytest.mark.parametrize("encoding", ["service", "canonical"])
def test_model_management_audits_keep_core_integrity_without_rewriting_history(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    action: str,
    payload: dict[str, Any],
    encoding: str,
) -> None:
    instance: NoyraService = request.getfixturevalue("management_service")[0]
    database = instance.kernel.database
    if encoding == "service":
        instance.http.audit_admin_event(action, "web-operator", payload)
    else:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    "audit_model_compatibility",
                    instance.kernel.subject_id,
                    action,
                    "web-operator",
                    canonical_json(payload),
                    "2026-10-14T00:00:00+00:00",
                ),
            )
    with database.connection() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
    report = IntegrityRegistry().run(
        database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()
    with database.connection() as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
    assert after == before


@pytest.mark.usefixtures("management_service")
@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("model_resource_discovery_succeeded", {"model_count": True}),
        ("model_resource_discovery_succeeded", {"model_count": -1}),
        ("model_resource_discovery_succeeded", {"model_count": 501}),
        ("model_resource_discovery_succeeded", {"model_count": "2"}),
        ("model_resource_discovery_succeeded", {"model_count": 2, "extra": "ignored"}),
        ("model_resource_draft_test_succeeded", {"model": None}),
        ("model_resource_draft_test_succeeded", {"model": []}),
        ("model_resource_draft_test_succeeded", {"model": " "}),
        ("model_resource_draft_test_succeeded", {"model": "x" * 257}),
        ("model_resource_draft_test_failed", {"error": " "}),
        ("model_resource_draft_test_failed", {"error": {"secret": "value"}}),
        ("model_resource_discovery_failed", {"error": "provider error with raw details"}),
        ("model_resource_discovery_rejected", {"error": "provider_auth_failed"}),
        ("model_resource_draft_test_rejected", {"error": "invalid_model_discovery"}),
        ("model_resource_discovery_unrecognized", {"model_count": 2}),
    ],
)
def test_model_management_audits_reject_invalid_summaries_and_unknown_actions(
    request: pytest.FixtureRequest, tmp_path: Path, action: str, payload: dict[str, Any]
) -> None:
    instance: NoyraService = request.getfixturevalue("management_service")[0]
    instance.http.audit_admin_event(action, "web-operator", payload)
    report = IntegrityRegistry().run(
        instance.kernel.database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.p0 == ("core.actions:integrity_error",), report.to_dict()


@pytest.mark.parametrize("encoding", ["service", "canonical"])
def test_search_management_history_keeps_integrity_and_original_bytes(
    tmp_path: Path, encoding: str
) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-search-management-history")
    try:
        summaries = (
            [
                ("search_routing_updated", {"mode": mode})
                for mode in ("model_first", "api_first", "auto")
            ]
            + [
                (
                    "search_provider_test_succeeded",
                    {"provider_type": provider, "result_count": count},
                )
                for provider in ("brave", "bing", "tavily", "serper")
                for count in (0, 3)
            ]
            + [
                ("search_provider_test_failed", {"provider_type": "brave", "error": error})
                for error in ("search_provider_test_failed", "search_provider_test_unknown")
            ]
        )
        with kernel.database.transaction() as connection:
            for index, (action, payload) in enumerate(summaries):
                connection.execute(
                    "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                    (
                        f"audit_search_history_{index}",
                        kernel.subject_id,
                        action,
                        "web-operator",
                        json.dumps(payload, sort_keys=True)
                        if encoding == "service"
                        else canonical_json(payload),
                        "2026-10-14T00:00:00+00:00",
                    ),
                )
        with kernel.database.connection() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert report.status == "ok", report.to_dict()
        with kernel.database.connection() as connection:
            after = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
        assert after == before
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("action", "payload", "actor"),
    [
        ("search_routing_updated", {"mode": "invalid"}, "web-operator"),
        ("search_routing_updated", {"mode": []}, "web-operator"),
        ("search_routing_updated", {"mode": "auto", "extra": 1}, "web-operator"),
        ("search_routing_updated", {"mode": "auto"}, " Subject "),
        ("search_routing_updated", {"mode": "auto"}, "x" * 257),
        *[
            (
                "search_provider_test_succeeded",
                {"provider_type": "brave", "result_count": count},
                "web-operator",
            )
            for count in (True, -1, 4, "1", 1.5, None)
        ],
        (
            "search_provider_test_succeeded",
            {"provider_type": "unknown", "result_count": 1},
            "web-operator",
        ),
        (
            "search_provider_test_succeeded",
            {"provider_type": [], "result_count": 1},
            "web-operator",
        ),
        ("search_provider_test_failed", {"provider_type": "brave"}, "web-operator"),
        (
            "search_provider_test_failed",
            {"provider_type": "brave", "error": "raw private error"},
            "web-operator",
        ),
        (
            "search_provider_test_failed",
            {"provider_type": "brave", "error": []},
            "web-operator",
        ),
    ],
)
def test_search_management_validator_rejects_invalid_contracts(
    action: str, payload: dict[str, Any], actor: str
) -> None:
    # These summaries intentionally need no database reference. Full registry
    # round trips and actual HTTP writers are tested separately below.
    row = {"audit_id": "audit_invalid_search", "action": action, "actor": actor}
    with pytest.raises(IntegrityError):
        _verify_management_audit_provenance(cast(Any, SimpleNamespace()), row, payload)


@pytest.mark.usefixtures("management_service")
def test_search_http_writers_keep_integrity_without_network_calls_or_saved_drafts(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, token, _ = request.getfixturevalue("management_service")
    probe = AsyncMock(
        side_effect=[
            {"ok": True, "provider_type": "brave", "result_count": 3},
            RuntimeError("private supplier error must not be audited"),
            SearchOutcomeUnknownError("private unknown response"),
        ]
    )
    monkeypatch.setattr(SearchExecutor, "test_unstored", probe)
    for expected_status in (200, 502, 502):
        status, _ = management_request(
            instance,
            "/api/config/search-providers/test",
            token=token,
            payload={"provider_type": "brave", "api_key": "search-secret-never-in-audit"},
        )
        assert status == expected_status
    for mode in ("model_first", "api_first", "auto"):
        status, _ = management_request(
            instance, "/api/config/search-routing", token=token, payload={"mode": mode}
        )
        assert status == 200
    assert probe.await_count == 3
    database = instance.kernel.database
    with database.connection() as connection:
        rows = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
        assert len(rows) == 6
        assert "private" not in repr(rows)
        assert "search-secret-never-in-audit" not in repr(rows)
        assert connection.execute("SELECT COUNT(*) FROM search_provider_configs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    report = IntegrityRegistry().run(
        database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()


@pytest.mark.usefixtures("management_service")
@pytest.mark.parametrize("length_fields", [False, True])
def test_public_controls_audits_accept_both_writer_versions_and_small_capacity(
    request: pytest.FixtureRequest, tmp_path: Path, length_fields: bool
) -> None:
    instance: NoyraService = request.getfixturevalue("management_service")[0]
    payload = {
        "rate_limit_per_hour": 10,
        "queue_cap": 50,
        "captcha_ttl_seconds": 300,
        "captcha_max_attempts": 3,
        "captcha_mode": "letters",
        "storage_cap_bytes": 1_000,
        "captcha_issue_limit_per_hour": 10,
        "captcha_global_rate_per_minute": 300,
    }
    if length_fields:
        payload.update(max_content_chars=100, length_revision=1)
    instance.http.audit_admin_event("public_post_controls_updated", "web-operator", payload)
    report = IntegrityRegistry().run(
        instance.kernel.database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()


@pytest.mark.usefixtures("management_service")
def test_model_draft_http_paths_preserve_integrity_without_real_provider_calls(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, token, _ = request.getfixturevalue("management_service")
    probe = AsyncMock(
        side_effect=[
            {"ok": True, "model": "draft-model"},
            ProviderCallError("provider_http_429", retryable=False, outcome_unknown=False),
        ]
    )
    discover = AsyncMock(
        side_effect=[
            ["draft-model", "other-model"],
            ProviderCallError("provider_auth_failed", retryable=False, outcome_unknown=False),
        ]
    )
    monkeypatch.setattr(OpenAICompatibleProvider, "probe_unstored", probe)
    monkeypatch.setattr(OpenAICompatibleProvider, "list_models_unstored", discover)
    payload: dict[str, object] = {
        "base_url": "https://models.example/v1",
        "model": "draft-model",
        "api_keys": ["draft-secret-never-in-audit"],
    }
    for endpoint in ("test", "models"):
        for expected_status in (200, 502):
            status, _ = management_request(
                instance,
                f"/api/config/model-resources/{endpoint}",
                token=token,
                payload=payload,
            )
            assert status == expected_status
        status, _ = management_request(
            instance, f"/api/config/model-resources/{endpoint}", token=token, payload={}
        )
        assert status == 400
    assert probe.await_count == discover.await_count == 2
    database = instance.kernel.database
    assert instance.http.cognitive_resources.list(instance.kernel.subject_id) == []
    with database.connection() as connection:
        rows = connection.execute("SELECT action, payload_json FROM audit_records").fetchall()
        assert len(rows) == 6
        assert "draft-secret-never-in-audit" not in repr([tuple(row) for row in rows])
    report = IntegrityRegistry().run(
        database,
        instance.kernel.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()


@pytest.mark.parametrize(
    ("payload_json", "actor"),
    [
        ('{"model_count": 1, "model_count": 2}', "web-operator"),
        ('{"model_count": NaN}', "web-operator"),
        ('{"model_count": Infinity}', "web-operator"),
        ('{ "model_count" : 2 }', "web-operator"),
        ('{"model_count":2}', "subject"),
    ],
)
def test_new_management_compatibility_keeps_malformed_json_and_subject_actor_blocked(
    tmp_path: Path, payload_json: str, actor: str
) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-model-management-damage")
    try:
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    "audit_malformed_model_summary",
                    kernel.subject_id,
                    "model_resource_discovery_succeeded",
                    actor,
                    payload_json,
                    "2026-10-14T00:00:00+00:00",
                ),
            )
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert report.p0 == ("core.actions:integrity_error",), report.to_dict()
    finally:
        kernel.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"max_content_chars": 100},
        {"length_revision": 1},
        {"max_content_chars": True, "length_revision": 1},
        {"max_content_chars": 0, "length_revision": 1},
        {"max_content_chars": 20_001, "length_revision": 1},
        {"max_content_chars": 100, "length_revision": 0},
        {"max_content_chars": 100, "length_revision": True},
        {"storage_cap_bytes": 999},
        {"storage_cap_bytes": 2_000_000_001},
        {"extra": 1},
    ],
)
def test_public_control_audit_compatibility_rejects_invalid_versions_and_limits(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-post-management-damage")
    try:
        payload = {
            "rate_limit_per_hour": 10,
            "queue_cap": 50,
            "captcha_ttl_seconds": 300,
            "captcha_max_attempts": 3,
            "captcha_mode": "letters",
            "storage_cap_bytes": 1_000,
            "captcha_issue_limit_per_hour": 10,
            "captcha_global_rate_per_minute": 300,
            **changes,
        }
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    "audit_invalid_control_summary",
                    kernel.subject_id,
                    "public_post_controls_updated",
                    "web-operator",
                    canonical_json(payload),
                    "2026-10-14T00:00:00+00:00",
                ),
            )
        report = IntegrityRegistry().run(
            kernel.database,
            kernel.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("core.actions",),
        )
        assert report.p0 == ("core.actions:integrity_error",), report.to_dict()
    finally:
        kernel.close()


@pytest.mark.usefixtures("management_service")
def test_management_post_and_inbound_audits_validate_owned_records(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    instance: NoyraService = request.getfixturevalue("management_service")[0]
    http = instance.http
    subject = instance.kernel.subject_id
    post = http.public_posts.create(
        subject,
        PublicPostInput(kind="help_request", title="验证", content="审计测试"),
        idempotency_key="audit-post",
        author_provenance="subject",
    )
    http.public_posts.moderate(
        post.post_id,
        subject_id=subject,
        status="published",
        actor="operator",
        reason="已审核",
    )
    http.audit_admin_event(
        "public_post_moderated",
        "operator",
        {
            "post_id": post.post_id,
            "operation": "publish",
            "reason": "已审核",
            "status": "published",
        },
    )
    http.audit_admin_event(
        "public_post_controls_updated",
        "operator",
        {
            "rate_limit_per_hour": 10,
            "queue_cap": 50,
            "captcha_ttl_seconds": 300,
            "captcha_max_attempts": 3,
            "captcha_mode": "letters",
            "storage_cap_bytes": 1_000_000,
            "captcha_issue_limit_per_hour": 10,
            "captcha_global_rate_per_minute": 300,
        },
    )
    transport = http.transports.configure(
        subject,
        TransportInput(
            channel="webhook",
            label="Audit inbound",
            endpoint="https://example.com/inbound",
            credentials={"webhook_secret": SecretStr("test-secret")},
        ),
        actor="operator",
    )
    http.inbound.bind(
        subject,
        transport.transport_id,
        external_account_id="account",
        external_sender_id="sender",
        role="participant",
    )
    accepted = http.inbound.ingest(
        InboundEnvelope(
            channel="webhook",
            transport_id=transport.transport_id,
            provider_event_id="audit-message",
            external_account_id="account",
            external_sender_id="sender",
            conversation_id="conversation",
            content="hello",
        )
    )
    http.audit_admin_event(
        "inbound_message_received",
        "transport:webhook",
        {
            "event_id": accepted.event_id,
            "interaction_id": accepted.interaction_id,
            "channel": "webhook",
            "duplicate": accepted.duplicate,
            "scheduling_priority": accepted.scheduling_priority,
        },
    )
    report = IntegrityRegistry().run(
        instance.kernel.database,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert report.status == "ok", report.to_dict()
    with instance.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_audit_record_update")
        row = connection.execute(
            "SELECT payload_json FROM audit_records WHERE action='inbound_message_received'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["interaction_id"] = "unrelated-interaction"
        connection.execute(
            "UPDATE audit_records SET payload_json=? WHERE action='inbound_message_received'",
            (json.dumps(payload, sort_keys=True),),
        )
    damaged = IntegrityRegistry().run(
        instance.kernel.database,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.actions",),
    )
    assert damaged.p0 == ("core.actions:integrity_error",)
