from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.errors import InvalidTransitionError
from noyra.core.types import content_hash
from noyra.mind import GoalCandidate, GoalStore
from noyra.wallet import (
    BountyInput,
    MockSigner,
    PaymentOrderRecord,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletAssetInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletStore,
)


@dataclass
class Payments:
    database: Database
    subject: str
    network_id: str
    asset_id: str
    goal_id: str
    now: str = "2026-11-15T12:00:00+00:00"
    economy: WalletEconomyStore = field(init=False)

    def __post_init__(self) -> None:
        self.economy = WalletEconomyStore(self.database, clock=lambda: self.now, production=False)

    def policy(self, proposal: PaymentPolicyInput) -> None:
        current = self.economy.get_policy(self.subject)
        self.economy.update_policy(
            self.subject,
            proposal,
            expected_version=current.policy_version,
            actor="operator",
        )

    def submit(self, key: str, *, amount: str = "5") -> str:
        now = datetime.fromisoformat(self.now)
        bounty = self.economy.create_bounty(
            self.subject,
            BountyInput(
                title="Verify payment limits",
                description="Provide reproducible evidence",
                acceptance_criteria=["proof"],
                network_id=self.network_id,
                asset_id=self.asset_id,
                reward_amount=amount,
                opens_at=(now - timedelta(minutes=1)).isoformat(),
                expires_at=(now + timedelta(days=90)).isoformat(),
                max_submissions=1,
                reward_slots=1,
                goal_id=self.goal_id,
                idempotency_key=key,
            ),
            actor="operator",
        )
        self.economy.publish_bounty(bounty.bounty_id, self.subject, actor="operator")
        return self.economy.submit(
            bounty.bounty_id,
            self.subject,
            SubmissionInput(
                counterparty="test-helper",
                content="proof",
                recipient_address="0xB111111111111111111111111111111111111111",
                idempotency_key=key,
                consent_version=1,
            ),
        ).submission_id

    def accept(self, submission_id: str) -> PaymentOrderRecord:
        self.economy.decide_submission(
            submission_id, self.subject, accepted=True, reason="verified", actor="operator"
        )
        order = self.economy.order_for_submission(submission_id, self.subject)
        assert order is not None
        return order


@pytest.fixture
def payments(tmp_path: Path) -> Payments:
    database = Database(tmp_path / "limits.sqlite3")
    subject = "Noyra-payment-limits"
    IdentityStore(database).ensure(subject, content_hash({"subject": subject}))
    event = EventStore(database).append(subject, "world_observation", "test", {"proof": True})
    goal = GoalStore(database).create_candidate(
        subject,
        GoalCandidate(
            title="Verify limits",
            description="Compare payment authorization boundaries",
            origin="self",
            priority=0.7,
            commitment=0.6,
            motive_emotion="curiosity",
        ),
        causal_source_ids=(event.event_id,),
        reason="independent payment policy verification",
    )
    wallets = WalletStore(database)
    network = wallets.register_network(
        subject,
        WalletNetworkInput(label="Test network", chain_id=1, native_symbol="ETH"),
        actor="operator",
    )
    asset = wallets.register_asset(
        subject,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Test Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    wallets.register_address(
        subject,
        WalletAddressInput(
            network_id=network.network_id,
            label="Test spending address",
            address="0xA111111111111111111111111111111111111111",
            purpose="spending",
        ),
        actor="operator",
    )
    setup = Payments(
        database,
        subject,
        network.network_id,
        asset.asset_id,
        goal.goal_id,
    )
    setup.policy(PaymentPolicyInput(mode="automatic", per_order_limit="5", daily_limit="100"))
    return setup


def test_automatic_uses_visible_caps_even_when_legacy_period_limits_are_lower(
    payments: Payments,
) -> None:
    payments.policy(
        PaymentPolicyInput(
            mode="automatic",
            per_order_limit="5",
            daily_limit="10",
            monthly_limit="1",
            daily_order_limit=1,
            monthly_order_limit=1,
            automatic_max_amount="1",
        )
    )
    assert payments.accept(payments.submit("first")).status == "reserved"
    assert payments.accept(payments.submit("at-daily-cap")).status == "reserved"
    assert payments.accept(payments.submit("over-daily-cap", amount="1")).status == "rejected"
    payments.now = "2026-11-16T12:00:00+00:00"
    assert payments.accept(payments.submit("over-single-cap", amount="6")).status == "rejected"
    assert payments.accept(payments.submit("new-day")).status == "reserved"
    payments.economy.verify_integrity(payments.subject)


@pytest.mark.parametrize("missing", ["per_order_limit", "daily_limit"])
def test_automatic_cannot_enable_with_an_unbounded_visible_cap(
    payments: Payments, missing: str
) -> None:
    current = payments.economy.get_policy(payments.subject)
    proposal = PaymentPolicyInput.model_validate(
        {"mode": "automatic", "per_order_limit": "5", "daily_limit": "10", missing: "0"}
    )
    with pytest.raises(ValueError, match="requires per-order and daily amount limits"):
        payments.policy(proposal)
    assert payments.economy.get_policy(payments.subject) == current
    payments.economy.verify_integrity(payments.subject)


@pytest.mark.parametrize(
    "limit",
    [
        {"daily_limit": "10"},
        {"monthly_limit": "10"},
        {"daily_order_limit": 2},
        {"monthly_order_limit": 2},
    ],
    ids=["daily-amount", "monthly-amount", "daily-count", "monthly-count"],
)
def test_automatic_orders_count_towards_later_confirmation_limits(
    payments: Payments, limit: dict[str, str | int]
) -> None:
    automatic = payments.accept(payments.submit("automatic"))
    assert automatic.status == "reserved" and automatic.payment_mode == "automatic"
    payments.policy(
        PaymentPolicyInput.model_validate(
            {
                "mode": "conditional_confirmation",
                "per_order_limit": "5",
                "anomaly_block": False,
                **limit,
            }
        )
    )
    second = payments.accept(payments.submit("confirmed-at-cap"))
    assert second.status == "awaiting_confirmation"
    assert second.payment_mode == "conditional_confirmation"
    assert (
        payments.economy.confirm_order(second.order_id, payments.subject, actor="operator").status
        == "reserved"
    )
    third = payments.accept(payments.submit("denied-over-cap", amount="1"))
    with pytest.raises(ValueError, match="policy denied reservation"):
        payments.economy.confirm_order(third.order_id, payments.subject, actor="operator")
    assert payments.economy.order_for_submission(third.submission_id, payments.subject) == third
    counts = payments.economy.verify_integrity(payments.subject)
    assert counts["wallet_ledger_journals"] == 2
    assert counts["wallet_ledger_entries"] == 4


@pytest.mark.parametrize(
    ("limit", "reset"),
    [
        ({"daily_limit": "5"}, "2026-11-16T00:00:00+00:00"),
        ({"daily_order_limit": 1}, "2026-11-16T00:00:00+00:00"),
        ({"monthly_limit": "5"}, "2026-12-01T00:00:00+00:00"),
        ({"monthly_order_limit": 1}, "2026-12-01T00:00:00+00:00"),
    ],
    ids=["daily-amount", "daily-count", "monthly-amount", "monthly-count"],
)
def test_shared_history_resets_at_the_authorization_period_boundary(
    payments: Payments, limit: dict[str, str | int], reset: str
) -> None:
    assert payments.accept(payments.submit("automatic-history")).status == "reserved"
    payments.policy(
        PaymentPolicyInput.model_validate(
            {"mode": "conditional_confirmation", "anomaly_block": False, **limit}
        )
    )
    pending = payments.accept(payments.submit("pending-before-reset", amount="1"))
    # Even across a different local date, this instant is still November 15 UTC.
    payments.now = "2026-11-16T01:59:59+02:00"
    with pytest.raises(ValueError, match="policy denied reservation"):
        payments.economy.confirm_order(pending.order_id, payments.subject, actor="operator")
    if "monthly_limit" in limit or "monthly_order_limit" in limit:
        payments.now = "2026-11-16T00:00:00+00:00"
        with pytest.raises(ValueError, match="policy denied reservation"):
            payments.economy.confirm_order(pending.order_id, payments.subject, actor="operator")
    payments.now = reset
    confirmed = payments.economy.confirm_order(pending.order_id, payments.subject, actor="operator")
    assert confirmed.status == "reserved"
    with payments.database.read_transaction() as connection:
        authorized_at = connection.execute(
            "SELECT authorized_at FROM wallet_payment_orders WHERE order_id=?",
            (confirmed.order_id,),
        ).fetchone()["authorized_at"]
    assert authorized_at == reset
    payments.economy.verify_integrity(payments.subject)


def test_concurrent_automatic_decisions_cannot_exceed_the_daily_amount_cap(
    payments: Payments,
) -> None:
    payments.policy(PaymentPolicyInput(mode="automatic", per_order_limit="5", daily_limit="10"))
    submissions = [payments.submit(f"concurrent-{index}", amount="4") for index in range(4)]
    start = threading.Barrier(4)

    def decide(submission_id: str) -> PaymentOrderRecord:
        start.wait(timeout=20)
        return payments.accept(submission_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(decide, submission) for submission in submissions]
        orders = [future.result(timeout=30) for future in futures]
    assert sorted(order.status for order in orders) == [
        "rejected",
        "rejected",
        "reserved",
        "reserved",
    ]
    assert sum(int(order.amount) for order in orders if order.status == "reserved") == 8
    counts = payments.economy.verify_integrity(payments.subject)
    assert counts["wallet_ledger_journals"] == 2
    assert counts["wallet_ledger_entries"] == 4


def test_replay_does_not_double_reserve_and_cancellation_releases_daily_budget(
    payments: Payments,
) -> None:
    payments.policy(PaymentPolicyInput(mode="automatic", per_order_limit="5", daily_limit="5"))
    submission = payments.submit("first")
    first = payments.accept(submission)
    assert first.status == "reserved"
    with pytest.raises(InvalidTransitionError, match="already decided"):
        payments.accept(submission)
    assert payments.economy.verify_integrity(payments.subject)["wallet_ledger_journals"] == 1
    assert payments.accept(payments.submit("cap-still-used", amount="1")).status == "rejected"
    assert (
        payments.economy.cancel_order(first.order_id, payments.subject, actor="operator").status
        == "cancelled"
    )
    assert payments.accept(payments.submit("budget-released")).status == "reserved"
    counts = payments.economy.verify_integrity(payments.subject)
    assert counts["wallet_ledger_journals"] == 3
    assert counts["wallet_ledger_entries"] == 6


@pytest.mark.parametrize("status", ["broadcast", "unknown", "confirmed"])
def test_sent_automatic_orders_keep_consuming_shared_period_budget(
    payments: Payments, status: str
) -> None:
    payments.policy(PaymentPolicyInput(mode="automatic", per_order_limit="5", daily_limit="5"))
    order = payments.accept(payments.submit("automatic-sent"))
    signer = MockSigner(chain_id=1, lose_first_response=status == "unknown")
    engine = WalletPaymentExecutionEngine(payments.database, signer, economy=payments.economy)
    sent = engine.execute_order(order.order_id, payments.subject, actor="operator")
    if status == "confirmed":
        assert sent.tx_hash is not None
        signer.set_receipt(sent.tx_hash, chain_id=1, status=1, block_number=7)
        sent = engine.poll_receipt(sent.execution_id, payments.subject, actor="operator")
    assert sent.status == status
    assert len(signer.requests) == 1
    assert payments.accept(payments.submit("automatic-over-cap", amount="1")).status == "rejected"
    payments.policy(
        PaymentPolicyInput(mode="conditional_confirmation", monthly_limit="5", anomaly_block=False)
    )
    pending = payments.accept(payments.submit("confirmation-over-cap", amount="1"))
    with pytest.raises(ValueError, match="policy denied reservation"):
        payments.economy.confirm_order(pending.order_id, payments.subject, actor="operator")
    payments.economy.verify_integrity(payments.subject)
