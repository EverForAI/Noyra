from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import DeliveryDispatcher, InteractionStore, TransportInput, TransportStore
from noyra.service import NoyraService
from test_m42_p3_06_operator_controls import _kernel
from test_m42_p3_06_operator_controls import service as service


@pytest.fixture
def delivery_case(
    tmp_path: Path,
) -> Iterator[tuple[SubjectKernel, TransportStore, DeliveryDispatcher]]:
    kernel = _kernel(tmp_path, "Noyra-m1-delivery")
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook", label="Synthetic destination", endpoint="https://example.com/send"
        ),
        actor="operator",
    )
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    try:
        yield kernel, transports, dispatcher
    finally:
        kernel.close()


def seed_delivery(
    kernel: SubjectKernel, dispatcher: DeliveryDispatcher, key: str, status: str = "unknown"
) -> str:
    interaction = InteractionStore(kernel.database).send(
        kernel.subject_id,
        "webhook",
        "synthetic-recipient",
        "Synthetic message " + key,
        idempotency_key=key,
    )
    dispatcher.enqueue_pending(kernel.subject_id)
    with kernel.database.transaction() as connection:
        delivery = connection.execute(
            "SELECT delivery_id FROM interaction_deliveries WHERE interaction_id=?",
            (interaction.interaction_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE interaction_deliveries SET status=?,attempts=1 WHERE delivery_id=?",
            (status, delivery),
        )
    return str(delivery)


def test_projection_rejects_reconciliation_on_wrong_original_state(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
) -> None:
    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "baseline")
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Synthetic receipt verified"
    )
    # A corrupted baseline must not be papered over by a perfectly hashed receipt.
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interaction_deliveries SET status='queued' WHERE delivery_id=?", (identifier,)
        )
    with pytest.raises(IntegrityError):
        dispatcher.get_delivery(identifier)
    with pytest.raises(IntegrityError):
        dispatcher.reconciliation_history(identifier, subject_id=kernel.subject_id)


def test_projection_rejects_cross_subject_receipt_even_when_rehashed(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
) -> None:
    from noyra.core import IdentityStore

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "ownership")
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Synthetic receipt verified"
    )
    IdentityStore(kernel.database).ensure("other-subject", content_hash("other-subject"))
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        row = dict(
            connection.execute(
                "SELECT * FROM interaction_delivery_reconciliations WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
        )
        state = {
            key: row[key]
            for key in (
                "reconciliation_id",
                "delivery_id",
                "subject_id",
                "sequence",
                "source",
                "outcome",
                "provider_message_id",
                "provider_status",
                "evidence_hash",
                "actor",
                "reason",
                "created_at",
            )
        }
        state["subject_id"] = "other-subject"
        connection.execute(
            "UPDATE interaction_delivery_reconciliations SET subject_id=?,state_hash=? "
            "WHERE delivery_id=?",
            ("other-subject", content_hash(state), identifier),
        )
    with pytest.raises(IntegrityError):
        dispatcher.get_delivery(identifier)


@pytest.mark.parametrize("outcome", ["delivered", "failed", "cancelled"])
def test_verified_effective_statuses_preserve_raw_rows_and_old_reconciliation_api(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
    outcome: str,
) -> None:
    from noyra.core.delivery_state import delivery_status_counts

    kernel, transports, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "settled")
    dispatcher._append_reconciliation(
        identifier,
        source="provider",
        outcome="error",
        provider_message_id=None,
        provider_status="temporary_failure",
        evidence={"lookup": "synthetic"},
        actor="operator",
        reason="Synthetic lookup",
        subject_id=kernel.subject_id,
    )
    with kernel.database.read_transaction() as connection:
        assert delivery_status_counts(connection, kernel.subject_id) == {"unknown": 1}
    resolved = dispatcher.reconcile_unknown(
        identifier,
        cast(Literal["delivered", "failed", "cancelled"], outcome),
        actor="operator",
        reason="Synthetic receipt",
        provider_message_id="synthetic-reference",
    )
    restarted = DeliveryDispatcher(kernel.database, transports)
    assert restarted.get_delivery(identifier) == resolved
    assert resolved.status == outcome and resolved.original_status == "unknown"
    assert resolved.provider_message_id == "synthetic-reference"
    assert len(restarted.reconciliation_history(identifier)) == 2
    with kernel.database.read_transaction() as connection:
        assert delivery_status_counts(connection, kernel.subject_id) == {outcome: 1}
        assert delivery_status_counts(connection, "unrelated-subject") == {}
        assert connection.total_changes == 0
        assert (
            connection.execute(
                "SELECT status FROM interaction_deliveries WHERE delivery_id=?", (identifier,)
            ).fetchone()[0]
            == "unknown"
        )
    assert (
        transports.verify_integrity(kernel.subject_id)["interaction_delivery_reconciliations"] == 2
    )


@pytest.mark.parametrize("damage", ["hash", "sequence", "duplicate_terminal"])
def test_invalid_receipts_never_settle_effective_counts(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
    damage: str,
) -> None:
    from noyra.core.delivery_state import delivery_status_counts

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "invalid")
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Synthetic receipt"
    )
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        if damage == "hash":
            connection.execute(
                "UPDATE interaction_delivery_reconciliations SET evidence_hash=? "
                "WHERE delivery_id=?",
                ("0" * 64, identifier),
            )
        else:
            row = dict(
                connection.execute(
                    "SELECT * FROM interaction_delivery_reconciliations WHERE delivery_id=?",
                    (identifier,),
                ).fetchone()
            )
            state = {
                key: row[key]
                for key in (
                    "reconciliation_id",
                    "delivery_id",
                    "subject_id",
                    "sequence",
                    "source",
                    "outcome",
                    "provider_message_id",
                    "provider_status",
                    "evidence_hash",
                    "actor",
                    "reason",
                    "created_at",
                )
            }
            if damage == "sequence":
                state["sequence"] = 2
                connection.execute(
                    "UPDATE interaction_delivery_reconciliations SET sequence=?,state_hash=? "
                    "WHERE delivery_id=?",
                    (2, content_hash(state), identifier),
                )
            else:
                connection.execute("DROP TRIGGER prevent_delivery_reconciliation_after_terminal")
                connection.execute("DROP INDEX uq_delivery_terminal_reconciliation")
                state.update(reconciliation_id="synthetic-second-terminal", sequence=2)
                row.update(**state, state_hash=content_hash(state))
                fields = list(row)
                connection.execute(
                    "INSERT INTO interaction_delivery_reconciliations("
                    + ",".join(fields)
                    + ") VALUES("
                    + ",".join("?" for _ in fields)
                    + ")",
                    tuple(row.values()),
                )
    with kernel.database.read_transaction() as connection, pytest.raises(IntegrityError):
        delivery_status_counts(connection, kernel.subject_id)
    with pytest.raises(IntegrityError):
        dispatcher.get_delivery(identifier)


@pytest.mark.parametrize("operation", ["wake", "reset"])
def test_reconciled_terminal_deliveries_no_longer_block_lifecycle_controls(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
    operation: str,
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
    from test_m42_p3_06_operator_controls import _put_kernel_to_deep_sleep

    kernel, transports, dispatcher = delivery_case
    controls = OperatorControlService(kernel)
    identifiers = [
        seed_delivery(kernel, dispatcher, "settle-" + outcome)
        for outcome in ("delivered", "failed", "cancelled")
    ]
    if operation == "wake":
        _put_kernel_to_deep_sleep(kernel)
    else:
        controls.pause(actor="operator", reason="Synthetic maintenance")
    method = controls.wake if operation == "wake" else controls.reset
    with pytest.raises(OperatorControlConflict, match=operation + "_blocked_by_recoverable_work"):
        method(actor="operator", reason="Still unresolved")
    for identifier, outcome in zip(identifiers, ("delivered", "failed", "cancelled"), strict=True):
        dispatcher.reconcile_unknown(
            identifier,
            cast(Literal["delivered", "failed", "cancelled"], outcome),
            actor="operator",
            reason="Synthetic evidence verified",
        )
    assert controls.recoverable_work()["counts"]["deliveries"] == 0
    assert controls.recoverable_work()["deliveries"] == []
    assert controls._work_health()["state"] == "ok"
    result = method(actor="operator", reason="All deliveries resolved")
    assert result.lifecycle["state"] == "active"
    with kernel.database.connection() as connection:
        assert {
            row[0] for row in connection.execute("SELECT status FROM interaction_deliveries")
        } == {"unknown"}
    assert (
        transports.verify_integrity(kernel.subject_id)["interaction_delivery_reconciliations"] == 3
    )


def test_pending_inventory_matches_effective_counts_and_does_not_hide_other_work(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
) -> None:
    from noyra.core.operator_controls import OperatorControlService

    kernel, _, dispatcher = delivery_case
    settled = seed_delivery(kernel, dispatcher, "old-settled")
    dispatcher.reconcile_unknown(settled, "delivered", actor="operator", reason="Verified")
    expected = {
        seed_delivery(kernel, dispatcher, status, status)
        for status in ("queued", "sending", "unknown")
    }
    action = kernel.action_ledger.prepare(
        kernel.subject_id, "synthetic", "test", "private-target", {}
    )
    controls = OperatorControlService(kernel)
    full = controls.recoverable_work(limit=10)
    assert full["counts"]["deliveries"] == 3
    assert {row["delivery_id"] for row in full["deliveries"]} == expected
    assert full["actions"] == [{"action_id": action.action_id, "status": "prepared"}]
    assert "private-target" not in str(full)
    small = controls.recoverable_work(limit=1)
    assert len(small["deliveries"]) == 1 and small["truncated"] is True
    assert small["counts"] == full["counts"]
    assert controls._work_health()["state"] == "attention"


def test_damaged_receipt_never_appears_as_healthy_or_unblocks_wake(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
    from test_m42_p3_06_operator_controls import _put_kernel_to_deep_sleep

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "damaged-control")
    dispatcher.reconcile_unknown(identifier, "delivered", actor="operator", reason="Verified")
    _put_kernel_to_deep_sleep(kernel)
    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        connection.execute(
            "UPDATE interaction_delivery_reconciliations SET evidence_hash=?", ("0" * 64,)
        )
    controls = OperatorControlService(kernel)
    assert controls._work_health() == {"state": "degraded", "reason": "unavailable"}
    with pytest.raises(IntegrityError):
        controls.recoverable_work()
    with pytest.raises(OperatorControlConflict, match="wake_work_state_unavailable"):
        controls.wake(actor="operator", reason="Corrupt receipt must not wake")
    assert kernel.lifecycle.current().state == "deep_sleep"


@pytest.mark.asyncio
async def test_reconciliation_does_not_authorize_resend_or_release_other_unknowns(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from noyra.core.operator_controls import OperatorControlService

    kernel, _, dispatcher = delivery_case
    identifiers = [
        seed_delivery(kernel, dispatcher, "no-resend-" + outcome)
        for outcome in ("failed", "cancelled")
    ]
    for identifier, outcome in zip(identifiers, ("failed", "cancelled"), strict=True):
        dispatcher.reconcile_unknown(
            identifier,
            cast(Literal["delivered", "failed", "cancelled"], outcome),
            actor="operator",
            reason="Verified",
        )
    unsettled = seed_delivery(kernel, dispatcher, "still-unknown")
    send = AsyncMock(side_effect=AssertionError("M1 must not resend any message"))
    monkeypatch.setattr(dispatcher, "_send", send)
    assert await dispatcher.deliver_pending(kernel.subject_id) == []
    send.assert_not_called()
    work = OperatorControlService(kernel).recoverable_work()
    assert work["deliveries"] == [{"delivery_id": unsettled, "status": "unknown"}]
    assert work["counts"]["deliveries"] == 1


def test_effective_counts_and_pending_list_share_a_read_snapshot(
    delivery_case: tuple[SubjectKernel, TransportStore, DeliveryDispatcher],
) -> None:
    from noyra.core.delivery_state import UNRESOLVED_DELIVERY_SQL, delivery_status_counts

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "snapshot")
    with kernel.database.read_transaction() as connection:
        assert delivery_status_counts(connection, kernel.subject_id) == {"unknown": 1}
        dispatcher.reconcile_unknown(
            identifier, "delivered", actor="operator", reason="Arrived during read"
        )
        rows = connection.execute(
            "SELECT d.delivery_id FROM interaction_deliveries d WHERE d.subject_id=? AND "
            + UNRESOLVED_DELIVERY_SQL,
            (kernel.subject_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [identifier]
        assert delivery_status_counts(connection, kernel.subject_id) == {"unknown": 1}
    with kernel.database.read_transaction() as connection:
        assert delivery_status_counts(connection, kernel.subject_id) == {"delivered": 1}


def test_operator_http_inventory_and_wake_use_verified_delivery_state(
    service: tuple[NoyraService, str, str],
) -> None:
    from test_m42_p3_06_operator_controls import _put_kernel_to_deep_sleep, _request

    instance, operator, read = service
    http = instance.http
    http.transports.configure(
        instance.kernel.subject_id,
        TransportInput(channel="webhook", label="Synthetic", endpoint="https://example.com/send"),
        actor="operator",
    )
    identifier = seed_delivery(instance.kernel, http.deliveries, "http-settled")
    for prefix in ("/api", "/api/v1"):
        assert _request(instance, prefix + "/admin/recoverable-work", token=read)[0] == 401
    status, result = _request(
        instance,
        "/api/v1/deliveries/" + identifier + "/reconcile",
        token=operator,
        payload={"status": "delivered", "reason": "Synthetic provider evidence"},
    )
    assert status == 200 and result["status"] == "delivered"
    status, records = _request(instance, "/api/deliveries", token=read)
    assert (
        status == 200
        and len(records) == 1
        and cast(list[dict[str, Any]], records)[0]["status"] == "delivered"
    )
    status, history = _request(
        instance, "/api/deliveries/" + identifier + "/reconciliations", token=read
    )
    assert status == 200 and cast(list[dict[str, Any]], history)[0]["outcome"] == "delivered"
    for prefix in ("/api", "/api/v1"):
        status, inventory = _request(instance, prefix + "/admin/recoverable-work", token=operator)
        assert status == 200
        assert inventory["deliveries"] == []
        assert cast(dict[str, int], inventory["counts"])["deliveries"] == 0
    _put_kernel_to_deep_sleep(instance.kernel)
    for prefix in ("/api", "/api/v1"):
        assert (
            _request(
                instance,
                prefix + "/admin/lifecycle/wake",
                token=read,
                payload={"reason": "Not allowed"},
            )[0]
            == 401
        )
    status, result = _request(
        instance, "/api/v1/admin/lifecycle/wake", token=operator, payload={"reason": "Settled work"}
    )
    assert status == 200 and cast(dict[str, object], result["lifecycle"])["state"] == "active"
    with instance.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        connection.execute(
            "UPDATE interaction_delivery_reconciliations SET state_hash=?", ("0" * 64,)
        )
    for prefix in ("/api", "/api/v1"):
        assert _request(instance, prefix + "/admin/recoverable-work", token=operator) == (
            503,
            {"error": "recoverable_work_unavailable"},
        )
        assert _request(instance, prefix + "/deliveries", token=read) == (
            503,
            {"error": "delivery_state_unavailable"},
        )
        assert _request(
            instance, prefix + "/deliveries/" + identifier + "/reconciliations", token=read
        ) == (503, {"error": "delivery_state_unavailable"})


def test_http_diagnostics_never_trust_corrupt_delivery_outcomes(
    service: tuple[NoyraService, str, str],
) -> None:
    from test_m42_p3_06_operator_controls import _request

    instance, _, read = service
    instance.http.transports.configure(
        instance.kernel.subject_id,
        TransportInput(channel="webhook", label="Synthetic", endpoint="https://example.com/send"),
        actor="operator",
    )
    identifier = seed_delivery(instance.kernel, instance.http.deliveries, "diagnostics")
    instance.http.deliveries.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Verified"
    )
    status, body = _request(instance, "/api/v1/diagnostics", token=read)
    assert status == 200 and body["deliveries"] == {"delivered": 1}
    with instance.kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        connection.execute(
            "UPDATE interaction_delivery_reconciliations SET evidence_json='{}',evidence_hash=?",
            ("0" * 64,),
        )
    for prefix in ("/api", "/api/v1"):
        assert _request(instance, prefix + "/diagnostics", token=read) == (
            503,
            {"error": "diagnostics_unavailable"},
        )
        assert _request(instance, prefix + "/deliveries", token=read) == (
            503,
            {"error": "delivery_state_unavailable"},
        )
        assert _request(
            instance, prefix + "/deliveries/" + identifier + "/reconciliations", token=read
        ) == (503, {"error": "delivery_state_unavailable"})
