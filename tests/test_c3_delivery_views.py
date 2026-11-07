from __future__ import annotations

from typing import Any

import pytest

from noyra.core.errors import IntegrityError
from noyra.core.runtime_observation import RuntimeObserver
from noyra.interaction import InteractionStore, PublicProjection
from noyra.interaction.conversations import ConversationStore
from test_delivery_effective_state import delivery_case as delivery_case
from test_delivery_effective_state import seed_delivery


def conversation_for(kernel: Any, delivery_id: str) -> str:
    from noyra.interaction.conversations import link_message

    window = ConversationStore(kernel.database).create(kernel.subject_id, "Synthetic delivery")
    with kernel.database.transaction() as c:
        row = c.execute(
            "SELECT i.* FROM interactions i JOIN interaction_deliveries d "
            "ON d.interaction_id=i.interaction_id WHERE d.delivery_id=?",
            (delivery_id,),
        ).fetchone()
        link_message(
            c,
            kernel.subject_id,
            row["interaction_id"],
            conversation_id=window["conversation_id"],
            related_id=None,
            channel=row["channel"],
            counterparty=row["counterparty"],
        )
    return str(window["conversation_id"])


@pytest.mark.parametrize("reader", ["conversation", "observation"])
def test_readers_never_trust_an_invalid_terminal_receipt(delivery_case: Any, reader: str) -> None:
    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "c3-corrupt")
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Synthetic receipt"
    )
    with kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        c.execute("UPDATE interaction_delivery_reconciliations SET state_hash=?", ("0" * 64,))
    if reader == "conversation":
        with pytest.raises(IntegrityError):
            ConversationStore(kernel.database).messages(
                kernel.subject_id, conversation_for(kernel, identifier)
            )
    else:
        result = RuntimeObserver(
            kernel.database.path, kernel.database.path.parent, kernel.subject_id
        ).snapshot(cognition_enabled=True, interval_seconds=30)
        assert result["database_observation"] != "ok"


def test_reconciled_failed_delivery_is_still_visible_for_attention(delivery_case: Any) -> None:
    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "c3-failed")
    dispatcher.reconcile_unknown(identifier, "failed", actor="operator", reason="Synthetic failure")
    result = RuntimeObserver(
        kernel.database.path, kernel.database.path.parent, kernel.subject_id
    ).snapshot(cognition_enabled=True, interval_seconds=30)
    assert result["attention"]["unresolved_deliveries"]["count"] == 1


def test_unscheduled_public_message_does_not_claim_external_delivery(delivery_case: Any) -> None:
    kernel, _, _ = delivery_case
    InteractionStore(kernel.database).send(
        kernel.subject_id,
        "public:forum",
        "world",
        "Synthetic public message",
        idempotency_key="c3-public",
    )
    row = PublicProjection(kernel.database).interactions_view(kernel.subject_id)[0]
    assert row["status"] == "sent"  # Preserve durable compatibility.
    assert row["communication_status"] == "not_scheduled"
    assert row["delivery_reason"] == "no_delivery_record"


@pytest.mark.parametrize("outcome", ["delivered", "failed", "cancelled"])
def test_valid_receipt_is_shared_by_conversation_and_summary_without_rewriting_intent(
    delivery_case: Any, outcome: str
) -> None:
    from noyra.core.delivery_state import delivery_attention_sample

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "c3-valid")
    dispatcher.reconcile_unknown(identifier, outcome, actor="operator", reason="Synthetic receipt")
    window = conversation_for(kernel, identifier)
    row = ConversationStore(kernel.database).messages(kernel.subject_id, window)["items"][0]
    assert row["status"] == "sent"
    assert row["communication_status"] == outcome
    assert row["delivery_statuses"] == [outcome]
    assert row["deliveries_truncated"] is False
    with kernel.database.read_transaction() as c:
        assert delivery_attention_sample(c, kernel.subject_id)["count"] == int(outcome == "failed")
        assert (
            c.execute(
                "SELECT status FROM interaction_deliveries WHERE delivery_id=?", (identifier,)
            ).fetchone()[0]
            == "unknown"
        )
        assert c.total_changes == 0


def test_local_web_reply_is_available_without_fabricated_delivery(delivery_case: Any) -> None:
    kernel, _, _ = delivery_case
    invitation = InteractionStore(kernel.database).receive(
        kernel.subject_id, "web", "web-user", "Synthetic invitation", idempotency_key="c3-web"
    )
    InteractionStore(kernel.database).send(
        kernel.subject_id,
        "web",
        "web-user",
        "Synthetic local reply",
        related_interaction_id=invitation.interaction_id,
        idempotency_key="c3-reply",
    )
    windows = ConversationStore(kernel.database).list(kernel.subject_id)
    rows = ConversationStore(kernel.database).messages(
        kernel.subject_id, windows["items"][0]["conversation_id"]
    )["items"]
    outgoing = next(row for row in rows if row["direction"] == "outgoing")
    assert outgoing["communication_status"] == "local_available"
    assert outgoing["delivery_statuses"] == []
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM interaction_deliveries").fetchone()[0] == 0


def test_attention_and_message_samples_are_explicitly_bounded(delivery_case: Any) -> None:
    from noyra.core.delivery_state import delivery_attention_sample
    from noyra.interaction.delivery_view import communication_view

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "c3-sample", status="failed")
    with kernel.database.transaction() as c:
        original = dict(
            c.execute(
                "SELECT * FROM interaction_deliveries WHERE delivery_id=?", (identifier,)
            ).fetchone()
        )
        columns = list(original)
        for index in range(101):
            values = {
                **original,
                "delivery_id": f"sample-{index}",
                "idempotency_key": f"sample-{index}",
            }
            c.execute(
                "INSERT INTO interaction_deliveries("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(values[key] for key in columns),
            )
    with kernel.database.read_transaction() as c:
        attention = delivery_attention_sample(c, kernel.subject_id)
        assert attention["sampled"] == 100 and attention["truncated"]
        assert attention["count"] == 50 and attention["more"]
        assert delivery_attention_sample(c, "other-subject")["count"] == 0
        message = c.execute(
            "SELECT * FROM interactions WHERE interaction_id=?", (original["interaction_id"],)
        ).fetchone()
        view = communication_view(c, message)
        assert view["communication_status"] == "delivery_sample_incomplete"
        assert len(view["delivery_statuses"]) == 10 and view["deliveries_truncated"]
        assert c.total_changes == 0


def test_read_side_checks_earlier_receipts_before_accepting_terminal(delivery_case: Any) -> None:
    from noyra.core.delivery_state import verified_delivery_status

    kernel, _, dispatcher = delivery_case
    identifier = seed_delivery(kernel, dispatcher, "c3-prior-receipt")
    dispatcher._append_reconciliation(
        identifier,
        source="provider",
        outcome="error",
        provider_message_id=None,
        provider_status="temporary_failure",
        evidence={},
        actor="operator",
        reason="Synthetic lookup",
        subject_id=kernel.subject_id,
    )
    dispatcher.reconcile_unknown(
        identifier, "delivered", actor="operator", reason="Synthetic receipt"
    )
    with kernel.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_delivery_reconciliation_update")
        c.execute(
            "UPDATE interaction_delivery_reconciliations SET evidence_hash=? WHERE sequence=1",
            ("0" * 64,),
        )
    with kernel.database.read_transaction() as c:
        row = c.execute(
            "SELECT * FROM interaction_deliveries WHERE delivery_id=?", (identifier,)
        ).fetchone()
        with pytest.raises(IntegrityError):
            verified_delivery_status(c, row)
