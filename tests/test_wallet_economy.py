from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.database import legacy_wallet_payment_policy_state_hash_v65
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
    economy: WalletEconomyStore,
    subject_id: str,
    network_id: str,
    asset_id: str,
    goal_id: str,
    *,
    key: str = "bounty-1",
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
            idempotency_key=key,
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
            asset_id=asset_id,
            address_id=address.address_id,
            balance="100",
            observed_at=(datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
        ),
        actor="operator",
    )
    wallets.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id, address_id=address.address_id, balance="250", observed_at=utc_now()
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


def test_automatic_policy_uses_only_daily_and_per_order_amount_limits(
    tmp_path: Path,
) -> None:
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
            asset_id=asset_id,
            address_id=address.address_id,
            balance="100",
            observed_at=(datetime.now(UTC) - timedelta(seconds=40)).isoformat(),
        ),
        actor="operator",
    )
    wallets.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id,
            address_id=address.address_id,
            balance="250",
            observed_at=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        ),
        actor="operator",
    )
    economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            per_order_limit="20",
            daily_limit="100",
            monthly_limit="20",
            daily_order_limit=1,
            monthly_order_limit=1,
            min_balance="249",
            max_observation_age_seconds=1,
            anomaly_block=True,
        ),
        expected_version=1,
        actor="operator",
    )
    goal_id = _goal(database, subject_id)
    bounties = [
        _bounty(economy, subject_id, network_id, asset_id, goal_id, key=f"daily-only-{index}")
        for index in (1, 2)
    ]
    for bounty in bounties:
        economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submissions = [
        economy.submit(
            bounty.bounty_id,
            subject_id,
            SubmissionInput(
                counterparty=f"human-{index}",
                content="proof",
                recipient_address=f"0x{index:040x}",
                idempotency_key=f"daily-only-submission-{index}",
                consent_version=1,
            ),
        )
        for index, bounty in enumerate(bounties, 1)
    ]
    for submission in submissions:
        economy.decide_submission(
            submission.submission_id,
            subject_id,
            accepted=True,
            reason="verified",
            actor="operator",
        )

    assert [order.status for order in economy.list_orders(subject_id)] == [
        "reserved",
        "reserved",
    ]


def test_automatic_payment_uses_explicit_caps_without_legacy_extra_cap(tmp_path: Path) -> None:
    database, subject_id, wallets, economy, network_id, asset_id = _fixture(tmp_path)
    wallets.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network_id,
            label="Spend",
            address="0xA111111111111111111111111111111111111111",
            purpose="spending",
        ),
        actor="operator",
    )
    policy = PaymentPolicyInput(
        mode="automatic",
        per_order_limit="20",
        daily_limit="100",
        automatic_max_amount="1",
        min_balance="1000",
    )
    economy.update_policy(subject_id, policy, expected_version=1, actor="operator")
    bounty = _bounty(
        economy, subject_id, network_id, asset_id, _goal(database, subject_id), key="unbounded-auto"
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(
        bounty.bounty_id,
        subject_id,
        SubmissionInput(
            counterparty="human",
            content="proof",
            recipient_address="0xB111111111111111111111111111111111111111",
            idempotency_key="unbounded-auto-submission",
            consent_version=1,
        ),
    )

    economy.decide_submission(
        submission.submission_id,
        subject_id,
        accepted=True,
        reason="verified",
        actor="operator",
    )

    order = economy.order_for_submission(submission.submission_id, subject_id)
    assert order is not None
    assert order.status == "reserved"


def test_recipient_allowlist_is_disabled_by_default_and_optional_when_enabled(
    tmp_path: Path,
) -> None:
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
    policy = economy.update_policy(
        subject_id,
        PaymentPolicyInput(mode="automatic", per_order_limit="20", daily_limit="100"),
        expected_version=1,
        actor="operator",
    )
    assert policy.recipient_allowlist_enabled is False
    assert policy.allowed_recipient_addresses == ()

    goal_id = _goal(database, subject_id)
    open_bounty = _bounty(economy, subject_id, network_id, asset_id, goal_id, key="allowlist-open")
    economy.publish_bounty(open_bounty.bounty_id, subject_id, actor="operator")
    open_submission = economy.submit(
        open_bounty.bounty_id,
        subject_id,
        SubmissionInput(
            counterparty="open-human",
            content="proof",
            recipient_address="0xB111111111111111111111111111111111111111",
            idempotency_key="allowlist-open-submission",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        open_submission.submission_id,
        subject_id,
        accepted=True,
        reason="verified",
        actor="operator",
    )
    open_order = economy.order_for_submission(open_submission.submission_id, subject_id)
    assert open_order is not None
    assert open_order.status == "reserved"

    policy = economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            per_order_limit="20",
            daily_limit="100",
            recipient_allowlist_enabled=True,
            allowed_recipient_addresses=["0xC111111111111111111111111111111111111111"],
        ),
        expected_version=policy.policy_version,
        actor="operator",
    )
    assert policy.recipient_allowlist_enabled is True
    assert policy.allowed_recipient_addresses == ("0xc111111111111111111111111111111111111111",)

    blocked_bounty = _bounty(
        economy, subject_id, network_id, asset_id, goal_id, key="allowlist-blocked"
    )
    economy.publish_bounty(blocked_bounty.bounty_id, subject_id, actor="operator")
    blocked_submission = economy.submit(
        blocked_bounty.bounty_id,
        subject_id,
        SubmissionInput(
            counterparty="blocked-human",
            content="proof",
            recipient_address="0xB211111111111111111111111111111111111111",
            idempotency_key="allowlist-blocked-submission",
            consent_version=1,
        ),
    )
    economy.decide_submission(
        blocked_submission.submission_id,
        subject_id,
        accepted=True,
        reason="verified",
        actor="operator",
    )
    blocked_order = economy.order_for_submission(blocked_submission.submission_id, subject_id)
    assert blocked_order is not None
    assert blocked_order.status == "rejected"


def test_recipient_policy_fields_migrate_and_rehash_existing_policy(tmp_path: Path) -> None:
    database, subject_id, _wallets, _economy, _network_id, _asset_id = _fixture(tmp_path)
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
            (
                legacy_wallet_payment_policy_state_hash_v65(
                    subject_id=subject_id,
                    mode=row["mode"],
                    allowed_network_ids_json=row["allowed_network_ids_json"],
                    allowed_asset_ids_json=row["allowed_asset_ids_json"],
                    per_order_limit=row["per_order_limit"],
                    daily_limit=row["daily_limit"],
                    monthly_limit=row["monthly_limit"],
                    daily_order_limit=row["daily_order_limit"],
                    monthly_order_limit=row["monthly_order_limit"],
                    min_balance=row["min_balance"],
                    max_observation_age_seconds=row["max_observation_age_seconds"],
                    automatic_max_amount=row["automatic_max_amount"],
                    anomaly_block=row["anomaly_block"],
                    emergency_paused=row["emergency_paused"],
                    policy_version=row["policy_version"],
                    updated_at=row["updated_at"],
                ),
                subject_id,
            ),
        )
        connection.execute("UPDATE schema_meta SET value='65' WHERE key='schema_version'")

    upgraded = Database(database.path)
    policy = WalletEconomyStore(upgraded).get_policy(subject_id)
    assert policy.recipient_allowlist_enabled is False
    assert policy.allowed_recipient_addresses == ()
    with upgraded.read_transaction() as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "66"
        )
    WalletEconomyStore(upgraded).verify_integrity(subject_id)
