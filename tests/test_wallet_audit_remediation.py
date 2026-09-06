from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from noyra.core import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.errors import IntegrityError
from noyra.wallet import (
    BountyInput,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletBalanceSnapshotInput,
    WalletEconomyStore,
    WalletPaymentExecutionEngine,
    WalletSignerError,
    WalletStore,
)
from test_wallet_execution import fixture


def test_disabled_policy_blocks_existing_reserved_order(tmp_path: Path) -> None:
    db, subject, network, asset, _source, order = fixture(tmp_path)
    economy = WalletEconomyStore(db)
    policy = economy.get_policy(subject)
    economy.update_policy(
        subject,
        PaymentPolicyInput(
            mode="disabled",
            allowed_network_ids=[network.network_id],
            allowed_asset_ids=[asset.asset_id],
        ),
        expected_version=policy.policy_version,
        actor="operator",
    )
    with pytest.raises(ValueError):
        WalletPaymentExecutionEngine(db, MockSigner()).execute_order(
            order.order_id, subject, actor="operator"
        )


def test_unknown_retry_honors_emergency_pause(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    signer = MockSigner(lose_first_response=True)
    engine = WalletPaymentExecutionEngine(db, signer)
    engine.execute_order(order.order_id, subject, actor="operator")
    policy = engine.economy.get_policy(subject)
    engine.economy.update_policy(
        subject,
        PaymentPolicyInput(
            mode=cast(Literal["disabled", "conditional_confirmation", "automatic"], policy.mode),
            allowed_network_ids=list(policy.allowed_network_ids),
            allowed_asset_ids=list(policy.allowed_asset_ids),
            per_order_limit=policy.per_order_limit,
            daily_limit=policy.daily_limit,
            monthly_limit=policy.monthly_limit,
            daily_order_limit=policy.daily_order_limit,
            monthly_order_limit=policy.monthly_order_limit,
            min_balance=policy.min_balance,
            max_observation_age_seconds=policy.max_observation_age_seconds,
            automatic_max_amount=policy.automatic_max_amount,
            anomaly_block=policy.anomaly_block,
            emergency_paused=True,
        ),
        expected_version=policy.policy_version,
        actor="operator",
    )
    with pytest.raises(ValueError):
        engine.retry_unknown(order.order_id, subject, actor="operator", reason="paused")
    assert len(signer.requests) == 1


def test_missing_block_receipt_cannot_settle(tmp_path: Path) -> None:
    db, subject, network, _asset, _source, order = fixture(tmp_path)
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    broadcast = engine.execute_order(order.order_id, subject, actor="operator")
    assert broadcast.tx_hash is not None
    signer.set_receipt(broadcast.tx_hash, chain_id=network.chain_id, status=1, block_number=None)
    result = engine.poll_receipt(broadcast.execution_id, subject, actor="operator")
    assert result.status == "unknown"


def test_order_timestamp_tampering_is_detected(tmp_path: Path) -> None:
    db, subject, _network, _asset, _source, _order = fixture(tmp_path)
    with db.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_wallet_order_identity_update")
        connection.execute(
            "UPDATE wallet_payment_orders SET created_at='2000-01-01T00:00:00+00:00'"
        )
    with pytest.raises(IntegrityError):
        WalletEconomyStore(db).verify_integrity(subject)


def test_prebroadcast_failure_can_reuse_chain_nonce(tmp_path: Path) -> None:
    db, subject, network, _asset, source, first = fixture(tmp_path)

    class RejectFirst(MockSigner):
        rejected = False

        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            if not self.rejected:
                self.rejected = True
                raise WalletSignerError("rejected before broadcast")
            return super().sign_and_broadcast(transfer, request_id=request_id)

    signer = RejectFirst(chain_id=network.chain_id, source_address=source.address)
    engine = WalletPaymentExecutionEngine(db, signer)
    assert engine.execute_order(first.order_id, subject, actor="operator").status == "failed"
    # The failed pre-broadcast attempt has no chain identity and must not burn
    # nonce zero in the durable uniqueness contract.
    economy = engine.economy
    now = datetime.now(UTC)
    bounty = economy.create_bounty(
        subject,
        BountyInput(
            title="Second",
            description="Second",
            acceptance_criteria=["proof"],
            network_id=network.network_id,
            asset_id=_asset.asset_id,
            reward_amount="1",
            opens_at=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            max_submissions=1,
            reward_slots=1,
            goal_id="goal_execution",
            idempotency_key="second-bounty",
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
            idempotency_key="second-submission",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, subject, accepted=True, reason="verified", actor="operator"
    )
    second = economy.order_for_submission(submission.submission_id, subject)
    assert second is not None
    # Use an explicit pending nonce authority response of zero.
    signer._pending_nonces[(source.address, network.chain_id)] = 0
    # The first failure is not a broadcast; the next operation may reuse zero.
    result = engine.execute_order(second.order_id, subject, actor="operator")
    assert result.nonce == 0 and result.status == "broadcast"


def test_future_balance_observation_is_not_payment_authority(tmp_path: Path) -> None:
    db, subject, _network, asset, source, order = fixture(tmp_path)
    wallets = WalletStore(db)
    wallets.record_balance_snapshot(
        subject,
        WalletBalanceSnapshotInput(
            asset_id=asset.asset_id,
            address_id=source.address_id,
            balance="100",
            source="test",
            observed_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        ),
        actor="operator",
    )
    with pytest.raises(ValueError):
        WalletPaymentExecutionEngine(db, MockSigner()).execute_order(
            order.order_id, subject, actor="operator"
        )


def test_invalidated_runtime_fences_signer_before_external_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, subject, _network, _asset, _source, order = fixture(tmp_path)
    signer = MockSigner()
    engine = WalletPaymentExecutionEngine(db, signer)
    gate = RuntimeAdmissionGate(subject)
    original_prepare = engine._prepare

    def invalidate_after_prepare(*args: Any, **kwargs: Any) -> Any:
        result = original_prepare(*args, **kwargs)
        gate.invalidate()
        return result

    monkeypatch.setattr(engine, "_prepare", invalidate_after_prepare)
    with (
        gate.operation("wallet-execution") as lease,
        bind_lease(lease),
        pytest.raises(OperationInvalidated),
    ):
        engine.execute_order(order.order_id, subject, actor="operator")
    assert signer.requests == []
    execution = engine._order_execution(order.order_id, subject)
    assert execution is not None and execution.status == "signing"
