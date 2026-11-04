from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import DeliveryDispatcher, InteractionStore, TransportInput, TransportStore
from test_m42_p3_06_operator_controls import _kernel


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
    from typing import Literal, cast

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
