from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from noyra.core.errors import IntegrityError, InvalidTransitionError
from noyra.core.types import utc_now
from noyra.wallet import (
    BountyInput,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAssetInput,
    WalletBalanceSnapshotInput,
    WalletEconomyStore,
    WalletExecutionError,
    WalletPaymentExecutionEngine,
    WalletStore,
)
from noyra.wallet.execution import WalletBroadcastUnknownError, WalletSignerError
from test_wallet_execution import fixture


def order_for(economy: WalletEconomyStore, subject: str, network: str, asset: str, key: str) -> Any:
    now = datetime.now(UTC)
    bounty = economy.create_bounty(
        subject,
        BountyInput(
            title=key,
            description=key,
            acceptance_criteria=["proof"],
            network_id=network,
            asset_id=asset,
            reward_amount="10",
            opens_at=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            max_submissions=1,
            reward_slots=1,
            goal_id="goal_execution",
            idempotency_key=key,
        ),
        actor="operator",
    )
    economy.publish_bounty(bounty.bounty_id, subject, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        subject,
        SubmissionInput(
            counterparty="human",
            content="done",
            recipient_address="0xB111111111111111111111111111111111111111",
            idempotency_key=key,
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, subject, accepted=True, reason="verified", actor="operator"
    )
    order = economy.order_for_submission(submission.submission_id, subject)
    assert order is not None and order.status == "reserved"
    return order


def setup_token(tmp_path: Path, *, min_balance: str = "0", max_age: int = 300) -> tuple[Any, ...]:
    db, subject, network, native, source, native_order = fixture(tmp_path)
    wallets = WalletStore(db)
    token = wallets.register_asset(
        subject,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="token",
            contract_address="0xC111111111111111111111111111111111111111",
            name="Test token",
            symbol="TST",
            decimals=6,
        ),
        actor="operator",
    )
    economy = WalletEconomyStore(db)
    economy.update_policy(
        subject,
        PaymentPolicyInput(
            mode="automatic",
            allowed_network_ids=[network.network_id],
            allowed_asset_ids=[native.asset_id, token.asset_id],
            min_balance=min_balance,
            max_observation_age_seconds=max_age,
        ),
        expected_version=economy.get_policy(subject).policy_version,
        actor="operator",
    )
    wallets.record_balance_snapshot(
        subject,
        WalletBalanceSnapshotInput(
            asset_id=token.asset_id,
            address_id=source.address_id,
            balance="1000000000",
        ),
        actor="operator",
    )
    order = order_for(economy, subject, network.network_id, token.asset_id, "token-first")
    return db, subject, network, native, source, native_order, token, order


def native_balance(values: tuple[Any, ...], balance: str, *, age: int = 0) -> None:
    db, subject, _network, native, source, *_ = values
    WalletStore(db).record_balance_snapshot(
        subject,
        WalletBalanceSnapshotInput(
            asset_id=native.asset_id,
            address_id=source.address_id,
            balance=balance,
            observed_at=(datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        ),
        actor="operator",
    )


@pytest.mark.parametrize(
    "condition", ["missing_asset", "missing_snapshot", "stale", "future", "reserved"]
)
def test_token_fee_admission_fails_before_signer(tmp_path: Path, condition: str) -> None:
    values = setup_token(tmp_path)
    db, subject, _network, native, _source, _native_order, _token, order = values
    if condition == "missing_asset":
        WalletStore(db).revoke_asset(
            native.asset_id, subject_id=subject, actor="operator", reason="test"
        )
    elif condition in {"stale", "future", "reserved"}:
        native_balance(
            values,
            "100005" if condition == "reserved" else "1000000",
            age=301 if condition == "stale" else -60 if condition == "future" else 0,
        )
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    with pytest.raises(WalletExecutionError):
        engine.execute_order(order.order_id, subject, actor="operator")
    assert not signer.requests
    assert engine._order_execution(order.order_id, subject) is None
    with db.connection() as c:
        assert (
            c.execute(
                "SELECT status FROM wallet_payment_orders WHERE order_id=?", (order.order_id,)
            ).fetchone()["status"]
            == "reserved"
        )


def test_token_units_are_not_native_reserve_units(tmp_path: Path) -> None:
    values = setup_token(tmp_path, min_balance="500000")
    db, subject, *_rest, order = values
    # 100000 fee plus the existing 10-wei native order; token reserve is
    # denominated in token atoms, not an additional 500000 wei.
    native_balance(values, "100010")
    result = WalletPaymentExecutionEngine(db, MockSigner(), max_fee_per_gas="1").execute_order(
        order.order_id, subject, actor="operator"
    )
    assert result.status == "broadcast"


def test_token_fee_snapshot_has_bounded_age_when_policy_age_is_disabled(tmp_path: Path) -> None:
    values = setup_token(tmp_path, max_age=0)
    db, subject, *_rest, order = values
    native_balance(values, "1000000", age=86401)
    with pytest.raises(WalletExecutionError):
        WalletPaymentExecutionEngine(db, MockSigner(), max_fee_per_gas="1").execute_order(
            order.order_id, subject, actor="operator"
        )


def test_concurrent_token_executions_cannot_share_fee_reservation(tmp_path: Path) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, _native_order, token, first = values
    economy = WalletEconomyStore(db)
    second = order_for(economy, subject, network.network_id, token.asset_id, "token-second")
    native_balance(values, "200005")  # Covers either fee, not both plus 10 reserved wei.
    barrier = Barrier(2)
    signer = MockSigner()

    def execute(order: Any, nonce: int) -> str:
        barrier.wait(timeout=10)
        try:
            return (
                WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
                .execute_order(order.order_id, subject, actor="operator", nonce=nonce)
                .status
            )
        except WalletExecutionError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(execute, first, 0)
        b = pool.submit(execute, second, 1)
        assert sorted([a.result(), b.result()]) == ["broadcast", "denied"]
    assert len(signer.requests) == 1


def test_unknown_retry_revalidates_native_fees_without_double_counting(tmp_path: Path) -> None:
    values = setup_token(tmp_path)
    db, subject, *_rest, order = values
    native_balance(values, "100010")
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    unknown = engine.execute_order(order.order_id, subject, actor="operator")
    assert unknown.status == "unknown"
    native_balance(values, "0")
    with pytest.raises(WalletExecutionError):
        engine.retry_unknown(order.order_id, subject, actor="operator", reason="retry")
    assert engine.get_execution(unknown.execution_id, subject).attempt_count == 1
    native_balance(values, "100010")
    retried = engine.retry_unknown(order.order_id, subject, actor="operator", reason="retry")
    assert retried.status == "broadcast"
    assert retried.nonce == unknown.nonce and retried.max_fee_per_gas == "1"


@pytest.mark.parametrize("receipt_status", [0, 1])
def test_terminal_execution_requires_new_native_balance(
    tmp_path: Path,
    receipt_status: Any,
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, _native_order, token, first = values
    second = order_for(
        WalletEconomyStore(db), subject, network.network_id, token.asset_id, "token-after-terminal"
    )
    native_balance(values, "100010")
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    broadcast = engine.execute_order(first.order_id, subject, actor="operator")
    assert broadcast.tx_hash is not None
    signer.set_receipt(
        broadcast.tx_hash, chain_id=network.chain_id, status=receipt_status, block_number=1
    )
    terminal = engine.poll_receipt(broadcast.execution_id, subject, actor="operator")
    assert terminal.status == ("confirmed" if receipt_status == 1 else "failed")
    with pytest.raises(WalletExecutionError):
        engine.execute_order(second.order_id, subject, actor="operator")
    native_balance(values, "100010")
    assert engine.execute_order(second.order_id, subject, actor="operator").status == "broadcast"


@pytest.mark.parametrize("override", [False, True])
def test_token_uses_signer_quote_or_explicit_bounded_operator_override(
    tmp_path: Path,
    override: bool,
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, source, _native_order, token, order = values
    native_balance(values, "360010")

    class QuotedSigner(MockSigner):
        def get_fee_quote(self, transfer: Any) -> tuple[int, str]:
            assert transfer.chain_id == network.chain_id
            assert transfer.source_address == source.address
            assert transfer.to_address == token.contract_address
            assert transfer.nonce == 0 and transfer.value == "0"
            return 120000, "3"

    signer = QuotedSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    result = engine.execute_order(
        order.order_id,
        subject,
        actor="operator",
        gas_limit=100000 if override else None,
        max_fee_per_gas="2" if override else None,
    )
    assert (result.gas_limit, result.max_fee_per_gas) == (
        (100000, "2") if override else (120000, "3")
    )


@pytest.mark.parametrize("known_hash", [False, True])
@pytest.mark.parametrize("receipt_status", [0, 1])
def test_rejected_retry_retains_unresolved_payment_and_fee_reservation(
    tmp_path: Path, known_hash: bool, receipt_status: Any
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, _native_order, token, first = values
    second = order_for(WalletEconomyStore(db), subject, network.network_id, token.asset_id, "next")
    native_balance(values, "100010")

    class RejectRetry(MockSigner):
        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            if request_id.endswith(":attempt:2"):
                raise WalletSignerError("retry rejected, initial transaction unresolved")
            result = super().sign_and_broadcast(transfer, request_id=request_id)
            raise WalletBroadcastUnknownError(tx_hash=result.tx_hash if known_hash else None)

    signer = RejectRetry()
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    initial = engine.execute_order(first.order_id, subject, actor="operator")
    assert initial.status == "unknown"
    retried = engine.retry_unknown(first.order_id, subject, actor="operator", reason="retry")
    assert retried.status == "unknown"
    assert retried.tx_hash == initial.tx_hash
    assert retried.attempt_count == 2
    with pytest.raises(WalletExecutionError):
        engine.execute_order(second.order_id, subject, actor="operator")
    with pytest.raises(InvalidTransitionError):
        WalletEconomyStore(db).refund_order(first.order_id, subject, actor="operator", reason="no")
    engine.verify_integrity(subject)
    if known_hash:
        assert initial.tx_hash is not None
        signer.set_receipt(initial.tx_hash, chain_id=network.chain_id, status=receipt_status)
        restarted = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
        settled = restarted.poll_receipt(initial.execution_id, subject, actor="operator")
        assert settled.status == ("confirmed" if receipt_status else "failed")
        assert settled.tx_hash == initial.tx_hash
        restarted.verify_integrity(subject)


@pytest.mark.parametrize("receipt_status", [0, 1])
def test_native_payment_cannot_reuse_pre_token_settlement_balance(
    tmp_path: Path, receipt_status: Any
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, native_order, _token, first = values
    native_balance(values, "100010")
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    sent = engine.execute_order(first.order_id, subject, actor="operator")
    assert sent.tx_hash is not None
    signer.set_receipt(sent.tx_hash, chain_id=network.chain_id, status=receipt_status)
    engine.poll_receipt(sent.execution_id, subject, actor="operator")
    with pytest.raises(WalletExecutionError):
        engine.execute_order(native_order.order_id, subject, actor="operator")
    native_balance(values, "21010")
    assert (
        engine.execute_order(native_order.order_id, subject, actor="operator").status == "broadcast"
    )


@pytest.mark.parametrize("receipt_status", [0, 1])
def test_receipt_of_earlier_attempt_is_reconciled_after_successful_retry(
    tmp_path: Path, receipt_status: Any
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, *_rest, order = values
    native_balance(values, "100010")
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    first = engine.execute_order(order.order_id, subject, actor="operator")
    retried = engine.retry_unknown(order.order_id, subject, actor="operator", reason="retry")
    assert first.tx_hash is not None and retried.tx_hash != first.tx_hash
    signer.set_receipt(first.tx_hash, chain_id=network.chain_id, status=receipt_status)
    result = engine.poll_receipt(first.execution_id, subject, actor="operator")
    assert result.status == ("confirmed" if receipt_status else "failed")
    assert result.tx_hash == first.tx_hash
    engine.verify_integrity(subject)


def test_restart_during_retry_retains_previous_hash_and_reservation(tmp_path: Path) -> None:
    values = setup_token(tmp_path)
    db, subject, network, *_rest, order = values
    native_balance(values, "100010")
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    first = engine.execute_order(order.order_id, subject, actor="operator")
    engine._prepare(
        order.order_id,
        subject,
        actor="operator",
        gas_limit=None,
        max_fee_per_gas=None,
        nonce=None,
        retry_unknown=True,
        reason="retry",
    )
    recovered = engine.recover_inflight(subject, actor="operator")
    assert len(recovered) == 1 and recovered[0].status == "unknown"
    assert recovered[0].tx_hash == first.tx_hash
    engine.verify_integrity(subject)
    assert first.tx_hash is not None
    signer.set_receipt(first.tx_hash, chain_id=network.chain_id, status=1)
    assert engine.poll_receipt(first.execution_id, subject, actor="operator").status == "confirmed"


@pytest.mark.parametrize("legacy_state", ["failed_retry", "refunded_unknown"])
def test_legacy_unresolved_terminal_state_blocks_spending_and_refund(
    tmp_path: Path, legacy_state: str
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, _native_order, token, first = values
    economy = WalletEconomyStore(db)
    second = order_for(economy, subject, network.network_id, token.asset_id, "next")
    native_balance(values, "100010")
    engine = WalletPaymentExecutionEngine(
        db, MockSigner(lose_first_response=True), max_fee_per_gas="1"
    )
    sent = engine.execute_order(first.order_id, subject, actor="operator")
    if legacy_state == "failed_retry":
        engine._prepare(
            first.order_id,
            subject,
            actor="operator",
            gas_limit=None,
            max_fee_per_gas=None,
            nonce=None,
            retry_unknown=True,
            reason="legacy",
        )
    # Encode the transitions emitted by the pre-fix implementation. Keep the
    # original schema guards and hashes; this is valid legacy data, not corruption.
    with db.transaction() as c:
        order = economy._order_row(c, first.order_id, subject)
        if legacy_state == "failed_retry":
            execution = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=?", (sent.execution_id,)
            ).fetchone()
            now = utc_now()
            state_hash = engine._execution_hash_values_from_row(
                execution, "failed", None, "signer_rejected", None, None, None, None, None, 2, now
            )
            economy._transition_order(c, order, "failed", "operator", "legacy rejection")
            c.execute(
                "UPDATE wallet_payment_executions SET status='failed',tx_hash=NULL,"
                "error_code='signer_rejected',updated_at=?,state_hash=? WHERE execution_id=?",
                (now, state_hash, sent.execution_id),
            )
            engine._finish_attempt(c, execution, "failed", None, "signer_rejected", now)
        else:
            economy._transition_order(c, order, "refunded", "operator", "legacy refund")
            economy._post_order_journal(
                c, order, "refund", ("reserved", "debit"), ("released", "credit")
            )
    engine.verify_integrity(subject)
    native_balance(values, "1000000")
    with pytest.raises(IntegrityError):
        engine.execute_order(second.order_id, subject, actor="operator")
    if legacy_state == "failed_retry":
        with pytest.raises(IntegrityError):
            economy.refund_order(first.order_id, subject, actor="operator", reason="unsafe")


def test_native_retry_rechecks_shared_fee_budget_without_mutating_attempt(tmp_path: Path) -> None:
    values = setup_token(tmp_path)
    db, subject, network, _native, _source, native_order, _token, token_order = values
    native_balance(values, "121010")
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    first = engine.execute_order(native_order.order_id, subject, actor="operator")
    second = engine.execute_order(token_order.order_id, subject, actor="operator")
    assert first.status == "unknown" and second.tx_hash is not None
    signer.set_receipt(second.tx_hash, chain_id=network.chain_id, status=1)
    engine.poll_receipt(second.execution_id, subject, actor="operator")
    with pytest.raises(WalletExecutionError):
        engine.retry_unknown(native_order.order_id, subject, actor="operator", reason="retry")
    assert engine.get_execution(first.execution_id, subject).attempt_count == 1
    native_balance(values, "21010")
    assert (
        engine.retry_unknown(
            native_order.order_id, subject, actor="operator", reason="retry"
        ).status
        == "broadcast"
    )


@pytest.mark.parametrize("fault", ["transport", "malformed"])
def test_retry_error_preserves_previous_transaction_for_receipt_only_recovery(
    tmp_path: Path, fault: str
) -> None:
    values = setup_token(tmp_path)
    db, subject, network, *_rest, order = values
    native_balance(values, "100010")

    class BrokenRetry(MockSigner):
        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            if request_id.endswith(":attempt:2"):
                if fault == "transport":
                    raise RuntimeError("transport interrupted")
                return object()
            return super().sign_and_broadcast(transfer, request_id=request_id)

    signer = BrokenRetry(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer, max_fee_per_gas="1")
    sent = engine.execute_order(order.order_id, subject, actor="operator")
    result = engine.retry_unknown(order.order_id, subject, actor="operator", reason="retry")
    assert result.status == "unknown" and result.tx_hash == sent.tx_hash
    economy = WalletEconomyStore(db)
    economy.update_policy(
        subject,
        PaymentPolicyInput(mode="disabled"),
        expected_version=economy.get_policy(subject).policy_version,
        actor="operator",
    )
    assert sent.tx_hash is not None
    signer.set_receipt(sent.tx_hash, chain_id=network.chain_id, status=1)
    assert (
        engine.retry_unknown(
            order.order_id, subject, actor="operator", reason="reconcile only"
        ).status
        == "confirmed"
    )
    engine.verify_integrity(subject)
