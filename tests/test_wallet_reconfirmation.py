from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash
from noyra.core.wallet_schema import wallet_entry_hash, wallet_journal_hash
from noyra.wallet import MockSigner, WalletPaymentExecutionEngine
from noyra.wallet.execution import WalletChainReorganizationError, WalletExecutionRecord
from test_wallet_execution import fixture as payment_fixture


@dataclass
class Payment:
    database: Database
    subject: str
    signer: MockSigner
    engine: WalletPaymentExecutionEngine
    execution: WalletExecutionRecord


@pytest.fixture
def payment(tmp_path: Path) -> Iterator[Payment]:
    database, subject, network, _asset, source, order = payment_fixture(tmp_path)
    signer = MockSigner(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(database, signer)
    execution = engine.execute_order(order.order_id, subject, actor="operator")
    assert execution.tx_hash is not None and execution.status == "broadcast"
    yield Payment(database, subject, signer, engine, execution)


def confirm(payment: Payment, block: int = 20) -> WalletExecutionRecord:
    original = payment.execution
    assert original.tx_hash is not None
    payment.signer.set_receipt(
        original.tx_hash, chain_id=original.chain_id, status=1, block_number=block
    )
    return payment.engine.poll_receipt(original.execution_id, payment.subject, actor="operator")


def reorganize(payment: Payment) -> WalletExecutionRecord:
    original = payment.execution
    assert original.tx_hash is not None
    payment.signer._receipts.pop(original.tx_hash)
    with pytest.raises(WalletChainReorganizationError):
        payment.engine.verify_confirmed_receipt(original.execution_id, payment.subject)
    return payment.engine.mark_reconcile_required(
        original.execution_id, payment.subject, actor="operator", reason="Canonical block changed"
    )


def journal_snapshot(payment: Payment) -> dict[str, list[dict[str, Any]]]:
    with payment.database.read_transaction() as connection:
        return {
            table: [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("wallet_ledger_journals", "wallet_ledger_entries")
        }


def payment_snapshot(payment: Payment) -> dict[str, list[dict[str, Any]]]:
    with payment.database.read_transaction() as connection:
        return {
            table: [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "wallet_payment_orders",
                "wallet_payment_executions",
                "wallet_payment_execution_attempts",
                "wallet_payment_reconciliation_events",
                "wallet_ledger_journals",
                "wallet_ledger_entries",
                "audit_records",
            )
        }


def assert_valid(payment: Payment) -> None:
    payment.engine.economy.verify_integrity(payment.subject)
    payment.engine.verify_integrity(payment.subject)


def test_reorg_reconfirmation_reuses_verified_journal_without_another_payment(
    payment: Payment,
) -> None:
    initial = confirm(payment)
    assert initial.status == "confirmed"
    journals = journal_snapshot(payment)
    assert len(journals["wallet_ledger_journals"]) == 2
    assert len(journals["wallet_ledger_entries"]) == 4
    assert reorganize(payment).status == "unknown"
    assert journal_snapshot(payment) == journals

    recovered = confirm(payment, 21)
    assert recovered.status == "confirmed" and recovered.receipt_block_number == 21
    assert recovered.attempt_count == initial.attempt_count == 1
    assert recovered.tx_hash == initial.tx_hash
    assert len(payment.signer.requests) == 1
    assert journal_snapshot(payment) == journals
    stable = payment_snapshot(payment)
    assert (
        payment.engine.poll_receipt(recovered.execution_id, payment.subject, actor="operator")
        == recovered
    )
    assert payment_snapshot(payment) == stable
    assert_valid(payment)


def test_reconfirmation_survives_database_reopen_and_multiple_reorganizations(
    payment: Payment,
) -> None:
    confirm(payment)
    journals = journal_snapshot(payment)
    for block in (21, 22):
        reorganize(payment)
        reopened = Database(payment.database.path, initialize=False)
        payment.engine = WalletPaymentExecutionEngine(reopened, payment.signer)
        recovered = confirm(payment, block)
        assert recovered.receipt_block_number == block and recovered.status == "confirmed"
        assert recovered.attempt_count == 1
        assert journal_snapshot(payment) == journals
        assert_valid(payment)
    assert len(payment.signer.requests) == 1


def test_concurrent_receipt_checks_commit_only_one_reconfirmation(payment: Payment) -> None:
    confirm(payment)
    reorganize(payment)
    original = payment.execution
    assert original.tx_hash is not None
    payment.signer.set_receipt(
        original.tx_hash, chain_id=original.chain_id, status=1, block_number=21
    )
    before = journal_snapshot(payment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: payment.engine.poll_receipt(
                    original.execution_id, payment.subject, actor="operator"
                ),
                range(2),
            )
        )
    assert all(result.status == "confirmed" for result in results)
    assert journal_snapshot(payment) == before
    assert len(payment.signer.requests) == 1
    with payment.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE subject_id=? "
                "AND action='wallet_payment_confirmed'",
                (payment.subject,),
            ).fetchone()[0]
            == 2
        )
    assert_valid(payment)


JOURNAL_DAMAGE = (
    "journal_hash",
    "journal_subject",
    "journal_order",
    "journal_network",
    "journal_asset",
    "journal_amount",
    "entry_hash",
    "entry_subject",
    "entry_order",
    "entry_amount",
    "entry_time",
    "entry_direction",
    "entry_account",
    "missing_entry",
    "extra_entry",
    "missing_journal",
    "missing_journal_and_reconciliation",
    "missing_reconciliation",
    "reconciliation_hash",
    "reconciliation_subject",
    "reconciliation_order",
    "reconciliation_tx",
    "execution_hash",
    "execution_amount",
)


def damage_evidence(payment: Payment, damage: str) -> None:
    """Simulate disk corruption; restore all SQL guards before calling production."""
    with payment.database.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        guards = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('wallet_ledger_journals','wallet_ledger_entries',"
            "'wallet_payment_reconciliation_events','wallet_payment_executions')"
        ).fetchall()
        for guard in guards:
            connection.execute('DROP TRIGGER "' + guard["name"].replace('"', '""') + '"')
        journal = dict(
            connection.execute(
                "SELECT * FROM wallet_ledger_journals WHERE order_id=? "
                "AND journal_type='settlement'",
                (payment.execution.order_id,),
            ).fetchone()
        )
        entry = dict(
            connection.execute(
                "SELECT * FROM wallet_ledger_entries WHERE journal_id=? AND account='reserved'",
                (journal["journal_id"],),
            ).fetchone()
        )
        if damage.startswith("journal_"):
            field = damage.removeprefix("journal_")
            key = {
                "subject": "subject_id",
                "order": "order_id",
                "network": "network_id",
                "asset": "asset_id",
                "amount": "amount",
                "hash": "state_hash",
            }[field]
            journal[key] = "9" if field == "amount" else "damaged"
            if field != "hash":
                journal["state_hash"] = wallet_journal_hash(journal)
            connection.execute(
                f"UPDATE wallet_ledger_journals SET {key}=?,state_hash=? WHERE journal_id=?",
                (journal[key], journal["state_hash"], journal["journal_id"]),
            )
        elif damage.startswith("entry_"):
            field = damage.removeprefix("entry_")
            key = {
                "subject": "subject_id",
                "order": "order_id",
                "amount": "amount",
                "time": "created_at",
                "direction": "direction",
                "account": "account",
                "hash": "state_hash",
            }[field]
            entry[key] = {
                "amount": "9",
                "time": "2026-01-01T00:00:00+00:00",
                "direction": "credit",
                "account": "available",
            }.get(field, "damaged")
            if field != "hash":
                entry["state_hash"] = wallet_entry_hash(entry)
            connection.execute(
                f"UPDATE wallet_ledger_entries SET {key}=?,state_hash=? WHERE entry_id=?",
                (entry[key], entry["state_hash"], entry["entry_id"]),
            )
        elif damage == "missing_entry":
            connection.execute(
                "DELETE FROM wallet_ledger_entries WHERE entry_id=?", (entry["entry_id"],)
            )
        elif damage == "extra_entry":
            entry.update(entry_id="extra-entry", account="released", direction="credit")
            entry["state_hash"] = wallet_entry_hash(entry)
            connection.execute(
                "INSERT INTO wallet_ledger_entries VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(entry.values()),
            )
        elif damage in {"missing_journal", "missing_journal_and_reconciliation"}:
            connection.execute(
                "DELETE FROM wallet_ledger_entries WHERE journal_id=?", (journal["journal_id"],)
            )
            connection.execute(
                "DELETE FROM wallet_ledger_journals WHERE journal_id=?", (journal["journal_id"],)
            )
            if damage == "missing_journal_and_reconciliation":
                connection.execute("DELETE FROM wallet_payment_reconciliation_events")
        elif damage == "missing_reconciliation":
            connection.execute("DELETE FROM wallet_payment_reconciliation_events")
        elif damage.startswith("reconciliation_"):
            event = dict(
                connection.execute("SELECT * FROM wallet_payment_reconciliation_events").fetchone()
            )
            field = damage.removeprefix("reconciliation_")
            key = {
                "subject": "subject_id",
                "order": "order_id",
                "tx": "tx_hash",
                "hash": "state_hash",
            }[field]
            event[key] = (
                "0x" + "f" * 64 if field == "tx" else "0" * 64 if field == "hash" else "damaged"
            )
            if field != "hash":
                event["state_hash"] = content_hash(
                    {k: v for k, v in event.items() if k != "state_hash"}
                )
            connection.execute(
                f"UPDATE wallet_payment_reconciliation_events SET {key}=?,state_hash=?",
                (event[key], event["state_hash"]),
            )
        else:
            execution = dict(
                connection.execute("SELECT * FROM wallet_payment_executions").fetchone()
            )
            if damage == "execution_hash":
                execution["state_hash"] = "0" * 64
            else:
                assert damage == "execution_amount"
                execution["amount"] = "9"
                execution["state_hash"] = payment.engine._execution_hash(execution)
            connection.execute(
                "UPDATE wallet_payment_executions SET amount=?,state_hash=? WHERE execution_id=?",
                (execution["amount"], execution["state_hash"], execution["execution_id"]),
            )
        for guard in guards:
            connection.execute(guard["sql"])
        connection.commit()


@pytest.mark.parametrize("damage", JOURNAL_DAMAGE)
def test_damaged_or_missing_historical_evidence_cannot_authorize_reconfirmation(
    payment: Payment, damage: str
) -> None:
    confirm(payment)
    reorganize(payment)
    damage_evidence(payment, damage)
    before = payment_snapshot(payment)
    with pytest.raises(
        IntegrityError, match=r"wallet .*settlement|wallet .*entry|wallet .*reconfirmation"
    ):
        confirm(payment, 21)
    assert payment_snapshot(payment) == before
    assert len(payment.signer.requests) == 1


def test_existing_settlement_without_reorganization_cannot_be_silently_reused(
    payment: Payment,
) -> None:
    with payment.database.transaction() as connection:
        order = connection.execute(
            "SELECT * FROM wallet_payment_orders WHERE order_id=?",
            (payment.execution.order_id,),
        ).fetchone()
        payment.engine.economy._post_order_journal(
            connection, order, "settlement", ("reserved", "debit"), ("paid", "credit")
        )
    before = payment_snapshot(payment)
    with pytest.raises(IntegrityError, match="no reconciliation evidence"):
        confirm(payment)
    assert payment_snapshot(payment) == before
    assert len(payment.signer.requests) == 1


def test_reconfirmation_failure_rolls_back_order_execution_attempt_and_audits(
    payment: Payment,
) -> None:
    confirm(payment)
    reorganize(payment)
    before = payment_snapshot(payment)
    with (
        patch.object(
            payment.engine, "_finish_attempt", side_effect=RuntimeError("synthetic commit fault")
        ),
        pytest.raises(RuntimeError, match="synthetic commit fault"),
    ):
        confirm(payment, 21)
    assert payment_snapshot(payment) == before
    assert confirm(payment, 21).status == "confirmed"
    assert journal_snapshot(payment) == {
        key: before[key] for key in ("wallet_ledger_journals", "wallet_ledger_entries")
    }
    assert len(payment.signer.requests) == 1
    assert_valid(payment)


@pytest.mark.parametrize("kind", ["missing", "wrong_chain", "unconfirmed", "failed"])
def test_missing_invalid_or_failed_receipts_never_reconfirm_or_resend(
    payment: Payment, kind: str
) -> None:
    confirm(payment)
    reorganize(payment)
    before = journal_snapshot(payment)
    original = payment.execution
    assert original.tx_hash is not None
    if kind != "missing":
        payment.signer.set_receipt(
            original.tx_hash,
            chain_id=original.chain_id + int(kind == "wrong_chain"),
            status=0 if kind == "failed" else 1,
            block_number=21,
            confirmations=0 if kind == "unconfirmed" else 1,
        )
    result = payment.engine.poll_receipt(original.execution_id, payment.subject, actor="operator")
    assert result.status == ("failed" if kind == "failed" else "unknown")
    assert journal_snapshot(payment) == before
    assert len(payment.signer.requests) == 1


def test_cross_subject_receipt_and_unauthorized_retry_are_rejected(payment: Payment) -> None:
    confirm(payment)
    reorganize(payment)
    IdentityStore(payment.database).ensure("other-subject", content_hash("other-subject"))
    before = payment_snapshot(payment)
    with pytest.raises(NotFoundError):
        payment.engine.poll_receipt(
            payment.execution.execution_id, "other-subject", actor="operator"
        )
    with pytest.raises(PermissionError):
        payment.engine.retry_unknown(
            payment.execution.order_id, payment.subject, actor="subject", reason="no authority"
        )
    assert payment_snapshot(payment) == before
    assert len(payment.signer.requests) == 1


def test_verified_canonical_receipt_on_explicit_retry_does_not_broadcast_again(
    payment: Payment,
) -> None:
    confirm(payment)
    reorganize(payment)
    original = payment.execution
    assert original.tx_hash is not None
    payment.signer.set_receipt(
        original.tx_hash, chain_id=original.chain_id, status=1, block_number=21
    )
    before = journal_snapshot(payment)
    result = payment.engine.retry_unknown(
        original.order_id,
        payment.subject,
        actor="operator",
        reason="Confirm canonical receipt first",
    )
    assert result.status == "confirmed" and result.attempt_count == 1
    assert len(payment.signer.requests) == 1
    assert journal_snapshot(payment) == before
    assert_valid(payment)


@pytest.mark.parametrize("kind", ["token_reorg", "token_earlier_attempt"])
def test_token_effect_receipt_reconfirmation_preserves_original_payment(
    tmp_path: Path, kind: str
) -> None:
    from test_wallet_fee_admission import native_balance, setup_token

    values = setup_token(tmp_path)
    database, subject, _network, _native, _source, _native_order, _token, order = values
    native_balance(values, "100010")
    signer = MockSigner(lose_first_response=kind == "token_earlier_attempt")
    engine = WalletPaymentExecutionEngine(database, signer, max_fee_per_gas="1")
    first = engine.execute_order(order.order_id, subject, actor="operator")
    assert first.tx_hash is not None
    expected_attempts = 1
    if kind == "token_earlier_attempt":
        retried = engine.retry_unknown(
            order.order_id, subject, actor="operator", reason="Explicit retry"
        )
        assert retried.tx_hash != first.tx_hash
        expected_attempts = 2
    value = Payment(database, subject, signer, engine, first)
    original = confirm(value)
    assert original.receipt_effect_hash is not None
    journals = journal_snapshot(value)
    reorganize(value)
    result = confirm(value, 21)
    assert result.status == "confirmed" and result.tx_hash == first.tx_hash
    assert result.attempt_count == expected_attempts
    assert len(signer.requests) == expected_attempts
    assert journal_snapshot(value) == journals
    assert_valid(value)


def test_initial_receipt_failure_rolls_back_settlement_before_idempotent_retry(
    payment: Payment,
) -> None:
    before = payment_snapshot(payment)
    with (
        patch.object(
            payment.engine, "_finish_attempt", side_effect=RuntimeError("first commit fault")
        ),
        pytest.raises(RuntimeError, match="first commit fault"),
    ):
        confirm(payment)
    assert payment_snapshot(payment) == before
    assert confirm(payment).status == "confirmed"
    assert len(journal_snapshot(payment)["wallet_ledger_journals"]) == 2
    assert len(payment.signer.requests) == 1
    assert_valid(payment)
