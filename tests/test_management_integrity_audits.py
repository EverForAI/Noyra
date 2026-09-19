from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from noyra.core.integrity import IntegrityRegistry
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import canonical_json
from noyra.interaction import InboundEnvelope, PublicPostInput, TransportInput
from noyra.service import NoyraService
from test_m42_p1_02_integrity_runtime import _active_kernel
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
