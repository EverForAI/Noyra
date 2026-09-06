from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import InvalidTransitionError
from noyra.core.types import content_hash, utc_now
from noyra.wallet import (
    BountyInput,
    BountyRecord,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletAssetInput,
    WalletBalanceSnapshotInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletStore,
)


def _fixture(tmp_path: Path) -> tuple[Database, str, WalletStore, WalletEconomyStore, str, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-economy-test"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    wallets = WalletStore(database)
    network = wallets.register_network(
        subject_id,
        WalletNetworkInput(
            label="Ethereum", chain_id=1, native_symbol="ETH", rpc_url="https://rpc.example"
        ),
        actor="operator",
    )
    asset = wallets.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    return (
        database,
        subject_id,
        wallets,
        WalletEconomyStore(database),
        network.network_id,
        asset.asset_id,
    )


def _goal(database: Database, subject_id: str) -> str:
    goal_id = "goal_economy_test"
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO goals("
            "goal_id,subject_id,title,description,origin,status,priority,commitment,"
            "progress,emotional_pressure,state_hash,current_revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (goal_id, subject_id, "Goal", "Goal", "self", "active", 1, 1, 0, 0, "x", 1, now, now),
        )
    return goal_id


def _bounty(
    economy: WalletEconomyStore, subject_id: str, network_id: str, asset_id: str, goal_id: str
) -> BountyRecord:
    now = datetime.now(UTC)
    return economy.create_bounty(
        subject_id,
        BountyInput(
            title="Verify a result",
            description="Provide reproducible evidence",
            acceptance_criteria=["evidence"],
            network_id=network_id,
            asset_id=asset_id,
            reward_amount="10",
            opens_at=now.isoformat(timespec="milliseconds"),
            expires_at=(now + timedelta(days=1)).isoformat(timespec="milliseconds"),
            max_submissions=3,
            reward_slots=1,
            goal_id=goal_id,
            idempotency_key="bounty-1",
        ),
        actor="operator",
    )


def test_bounty_lifecycle(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network_id, asset_id = _fixture(tmp_path)
    bounty = _bounty(economy, subject_id, network_id, asset_id, _goal(database, subject_id))
    assert (
        economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator").status == "published"
    )
    with pytest.raises(InvalidTransitionError):
        economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")


def test_accepted_submission_creates_one_disabled_order(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network_id, asset_id = _fixture(tmp_path)
    bounty = _bounty(economy, subject_id, network_id, asset_id, _goal(database, subject_id))
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        subject_id,
        SubmissionInput(
            counterparty="human",
            content="proof",
            recipient_address="0xA111111111111111111111111111111111111111",
            idempotency_key="submission-1",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, subject_id, accepted=True, reason="verified", actor="operator"
    )
    orders = economy.list_orders(subject_id)
    assert len(orders) == 1 and orders[0].status == "rejected"
    with pytest.raises(InvalidTransitionError):
        economy.decide_submission(
            submission.submission_id, subject_id, accepted=True, reason="again", actor="operator"
        )


def test_automatic_policy_reserves_and_releases_once(tmp_path: Path) -> None:
    database, subject_id, wallets, economy, network_id, asset_id = _fixture(tmp_path)
    address = wallets.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network_id,
            label="Spend",
            address="0xA111111111111111111111111111111111111111",
            purpose="spending",
        ),
        actor="operator",
    )
    wallets.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id, address_id=address.address_id, balance="100", observed_at=utc_now()
        ),
        actor="operator",
    )
    economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            allowed_network_ids=[network_id],
            allowed_asset_ids=[asset_id],
            per_order_limit="20",
            daily_limit="100",
            monthly_limit="200",
            automatic_max_amount="20",
        ),
        expected_version=1,
        actor="operator",
    )
    bounty = _bounty(economy, subject_id, network_id, asset_id, _goal(database, subject_id))
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        subject_id,
        SubmissionInput(
            counterparty="human",
            content="proof",
            recipient_address="0xA111111111111111111111111111111111111111",
            idempotency_key="submission-1",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        submission.submission_id, subject_id, accepted=True, reason="verified", actor="operator"
    )
    order = economy.list_orders(subject_id)[0]
    assert order.status == "reserved"
    economy.cancel_order(order.order_id, subject_id, actor="operator")
    assert sum(int(balance.net) for balance in economy.ledger_balances(subject_id)) == 0
    assert economy.verify_integrity(subject_id)["wallet_ledger_entries"] == 4
