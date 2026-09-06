from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.database import wallet_payment_policy_state_hash
from noyra.core.errors import IntegrityError
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash
from noyra.core.wallet_schema import wallet_journal_hash
from noyra.wallet import (
    BountyInput,
    MockSigner,
    PaymentPolicyInput,
    SubmissionInput,
    WalletAddressInput,
    WalletAssetInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletStore,
)

SOURCE_ADDRESS = "0xA111111111111111111111111111111111111111"


def _goal(database: Database, subject_id: str, goal_id: str = "goal-release") -> str:
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO goals(goal_id,subject_id,title,description,origin,status,priority,"
            "commitment,progress,emotional_pressure,state_hash,current_revision,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                goal_id,
                subject_id,
                "Release gate goal",
                "Release gate goal",
                "self",
                "active",
                1,
                1,
                0,
                0,
                "g" * 64,
                1,
                now,
                now,
            ),
        )
    return goal_id


def _fixture(
    tmp_path: Path,
    *,
    daily_limit: str = "0",
    per_order_limit: str = "100",
    emergency_paused: bool = False,
) -> tuple[Database, str, WalletStore, WalletEconomyStore, Any, Any, Any]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-wallet-release-gates"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    wallets = WalletStore(database)
    network = wallets.register_network(
        subject_id,
        WalletNetworkInput(
            label="Release Ethereum",
            chain_id=1,
            native_symbol="ETH",
            rpc_url="https://rpc.example",
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
    source = wallets.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network.network_id,
            label="Release spending",
            address=SOURCE_ADDRESS,
            purpose="spending",
        ),
        actor="operator",
    )
    economy = WalletEconomyStore(database)
    economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            allowed_network_ids=[network.network_id],
            allowed_asset_ids=[asset.asset_id],
            per_order_limit=per_order_limit,
            daily_limit=daily_limit,
            automatic_max_amount=per_order_limit,
            emergency_paused=emergency_paused,
        ),
        expected_version=1,
        actor="operator",
    )
    return database, subject_id, wallets, economy, network, asset, source


def _bounty(
    economy: WalletEconomyStore,
    subject_id: str,
    network_id: str,
    asset_id: str,
    goal_id: str,
    *,
    amount: str = "7",
    max_submissions: int = 2,
    reward_slots: int = 1,
    key: str = "release-bounty",
) -> Any:
    now = datetime.now(UTC)
    return economy.create_bounty(
        subject_id,
        BountyInput(
            title="Release gate bounty",
            description="Submit independently reproducible evidence.",
            acceptance_criteria=["evidence is reproducible"],
            network_id=network_id,
            asset_id=asset_id,
            reward_amount=amount,
            opens_at=(now - timedelta(minutes=1)).isoformat(timespec="milliseconds"),
            expires_at=(now + timedelta(days=1)).isoformat(timespec="milliseconds"),
            max_submissions=max_submissions,
            reward_slots=reward_slots,
            goal_id=goal_id,
            idempotency_key=key,
        ),
        actor="operator",
    )


def _submission(index: int) -> SubmissionInput:
    return SubmissionInput(
        counterparty=f"human-{index}@example.test",
        content="reproducible release evidence",
        evidence=["sha256:release-gate"],
        recipient_address="0x" + format(index, "040x"),
        idempotency_key=f"release-submission-{index}",
        consent_version=1,
    )


def test_concurrent_acceptance_is_capacity_safe_and_creates_one_order(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        reward_slots=1,
        key="concurrent-slots",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submissions = [
        economy.submit(bounty.bounty_id, subject_id, _submission(index)) for index in (1, 2)
    ]
    barrier = threading.Barrier(2)

    def decide(submission_id: str) -> object:
        barrier.wait(timeout=5)
        try:
            return economy.decide_submission(
                submission_id,
                subject_id,
                accepted=True,
                reason="release evidence verified",
                actor="operator",
            )
        except Exception as error:  # assertions below classify the expected loser
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, (item.submission_id for item in submissions)))

    accepted = [item for item in results if getattr(item, "status", None) == "accepted"]
    rejected = [item for item in results if isinstance(item, ValueError)]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert "reward slots" in str(rejected[0])
    orders = economy.list_orders(subject_id)
    assert len(orders) == 1 and orders[0].status == "reserved"
    assert economy.verify_integrity(subject_id)["wallet_ledger_journals"] == 1


def test_concurrent_policy_limit_cannot_be_exceeded(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(
        tmp_path, daily_limit="10"
    )
    goal_id = _goal(database, subject_id)
    bounty = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        reward_slots=2,
        key="concurrent-daily-limit",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submissions = [
        economy.submit(bounty.bounty_id, subject_id, _submission(index)) for index in (3, 4)
    ]
    barrier = threading.Barrier(2)

    def decide(submission_id: str) -> object:
        barrier.wait(timeout=5)
        try:
            return economy.decide_submission(
                submission_id,
                subject_id,
                accepted=True,
                reason="release evidence verified",
                actor="operator",
            )
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, (item.submission_id for item in submissions)))

    assert all(getattr(item, "status", None) == "accepted" for item in results)
    orders = economy.list_orders(subject_id)
    assert sorted(order.status for order in orders) == ["rejected", "reserved"]
    assert sum(int(order.amount) for order in orders if order.status == "reserved") <= 10
    assert economy.verify_integrity(subject_id)["wallet_ledger_journals"] == 1


def test_emergency_pause_fails_closed_for_existing_and_new_orders(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        key="pause-existing",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(bounty.bounty_id, subject_id, _submission(5))
    accepted = economy.decide_submission(
        submission.submission_id,
        subject_id,
        accepted=True,
        reason="verified before pause",
        actor="operator",
    )
    order = economy.order_for_submission(accepted.submission_id, subject_id)
    assert order is not None and order.status == "reserved"

    policy = economy.get_policy(subject_id)
    paused = economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            allowed_network_ids=[network.network_id],
            allowed_asset_ids=[asset.asset_id],
            per_order_limit="100",
            automatic_max_amount="100",
            emergency_paused=True,
        ),
        expected_version=policy.policy_version,
        actor="operator",
    )
    assert paused.emergency_paused
    engine = WalletPaymentExecutionEngine(
        database,
        MockSigner(chain_id=network.chain_id, source_address=source.address),
        economy=economy,
    )
    with pytest.raises(ValueError, match="emergency paused"):
        engine.execute_order(order.order_id, subject_id, actor="operator")
    assert engine.list_executions(subject_id) == []

    second = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        amount="3",
        max_submissions=1,
        key="pause-new",
    )
    economy.publish_bounty(second.bounty_id, subject_id, actor="operator")
    new_submission = economy.submit(second.bounty_id, subject_id, _submission(6))
    decided = economy.decide_submission(
        new_submission.submission_id,
        subject_id,
        accepted=True,
        reason="accepted while paused",
        actor="operator",
    )
    new_order = economy.order_for_submission(decided.submission_id, subject_id)
    assert new_order is not None and new_order.status == "rejected"


class _NoFetchallCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        raise AssertionError("wallet economy history must stream rows")


class _NoFetchallConnection:
    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, *args: Any, **kwargs: Any) -> _NoFetchallCursor:
        return _NoFetchallCursor(self._connection.execute(*args, **kwargs))


class _NoFetchallDatabase(Database):
    @contextmanager
    def connection(self) -> Iterator[Any]:
        with super().connection() as connection:
            yield _NoFetchallConnection(connection)

    @contextmanager
    def read_transaction(self) -> Iterator[Any]:
        with super().read_transaction() as connection:
            yield _NoFetchallConnection(connection)


def test_economy_integrity_and_balances_stream_large_history(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    count = {"pressure": 64, "soak": 128}.get(os.getenv("NOYRA_STAGE4B4_PROFILE", ""), 16)
    bounty = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        amount="1",
        max_submissions=count,
        reward_slots=count,
        key="streaming-history",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    for index in range(10, 10 + count):
        submission = economy.submit(bounty.bounty_id, subject_id, _submission(index))
        economy.decide_submission(
            submission.submission_id,
            subject_id,
            accepted=True,
            reason="verified",
            actor="operator",
        )

    bounded = WalletEconomyStore(_NoFetchallDatabase(database.path, initialize=False))
    counts = bounded.verify_integrity(subject_id)
    assert counts["wallet_ledger_journals"] == count
    assert counts["wallet_ledger_entries"] == count * 2
    assert {balance.account for balance in bounded.ledger_balances(subject_id)} == {
        "available",
        "reserved",
    }


def test_ledger_projections_use_bounded_subject_and_journal_plans(tmp_path: Path) -> None:
    database, subject_id, _wallets, _economy, _network, _asset, _source = _fixture(tmp_path)
    with database.connection() as connection:
        balance_plan = [
            str(row["detail"])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT account,direction,amount FROM wallet_ledger_entries WHERE subject_id=?",
                (subject_id,),
            )
        ]
        history_plan = [
            str(row["detail"])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT j.journal_id,j.created_at,e.entry_id "
                "FROM wallet_ledger_journals AS j "
                "LEFT JOIN wallet_payment_orders AS o ON o.order_id=j.order_id "
                "LEFT JOIN wallet_ledger_entries AS e ON e.journal_id=j.journal_id "
                "WHERE j.subject_id=? ORDER BY j.created_at,j.journal_id",
                (subject_id,),
            )
        ]
    assert any("idx_wallet_ledger_entries_subject" in detail for detail in balance_plan)
    assert any("idx_wallet_ledger_subject" in detail for detail in history_plan)
    assert any("idx_wallet_ledger_entries_journal_entry" in detail for detail in history_plan)
    assert not any("TEMP B-TREE" in detail for detail in history_plan)


def test_economy_integrity_rejects_cross_row_and_hash_tampering(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(economy, subject_id, network.network_id, asset.asset_id, goal_id)
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(bounty.bounty_id, subject_id, _submission(77))
    accepted = economy.decide_submission(
        submission.submission_id, subject_id, accepted=True, reason="verified", actor="operator"
    )
    order = economy.order_for_submission(accepted.submission_id, subject_id)
    assert order is not None
    with database.transaction() as connection:
        journal = connection.execute(
            "SELECT * FROM wallet_ledger_journals WHERE order_id=?",
            (order.order_id,),
        ).fetchone()
        assert journal is not None
        connection.execute("DROP TRIGGER prevent_wallet_journal_update")
        connection.execute(
            "UPDATE wallet_ledger_journals SET state_hash=? WHERE journal_id=?",
            ("0" * 64, journal["journal_id"]),
        )
    with pytest.raises(IntegrityError, match="journal hash mismatch"):
        economy.verify_integrity(subject_id)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE wallet_ledger_journals SET state_hash=? WHERE journal_id=?",
            (
                journal["state_hash"],
                journal["journal_id"],
            ),
        )
        connection.execute(
            "UPDATE wallet_ledger_journals SET amount=?, state_hash=? WHERE journal_id=?",
            (
                "2",
                wallet_journal_hash(dict(journal) | {"amount": "2"}),
                journal["journal_id"],
            ),
        )
    with pytest.raises(IntegrityError, match="order reference"):
        economy.verify_integrity(subject_id)


def test_economy_integrity_rejects_payment_policy_tampering(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, _network, _asset, _source = _fixture(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE wallet_payment_policies SET daily_limit=? WHERE subject_id=?",
            ("999", subject_id),
        )
    with pytest.raises(IntegrityError, match="payment policy hash mismatch"):
        economy.verify_integrity(subject_id)


def test_economy_integrity_rejects_ledger_entry_hash_tampering(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, _source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(economy, subject_id, network.network_id, asset.asset_id, goal_id)
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(bounty.bounty_id, subject_id, _submission(78))
    accepted = economy.decide_submission(
        submission.submission_id, subject_id, accepted=True, reason="verified", actor="operator"
    )
    order = economy.order_for_submission(accepted.submission_id, subject_id)
    assert order is not None
    with database.transaction() as connection:
        entry = connection.execute(
            "SELECT entry_id FROM wallet_ledger_entries WHERE order_id=? LIMIT 1",
            (order.order_id,),
        ).fetchone()
        assert entry is not None
        connection.execute("DROP TRIGGER prevent_wallet_entry_update")
        connection.execute(
            "UPDATE wallet_ledger_entries SET state_hash=? WHERE entry_id=?",
            ("0" * 64, entry["entry_id"]),
        )
    with pytest.raises(IntegrityError, match="entry hash mismatch"):
        economy.verify_integrity(subject_id)


def test_policy_hash_covers_all_durable_fields(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, _network, _asset, _source = _fixture(tmp_path)
    policy = economy.get_policy(subject_id)
    with database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
        ).fetchone()
    assert row is not None
    assert row["state_hash"] == wallet_payment_policy_state_hash(
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
        policy_version=policy.policy_version,
        updated_at=row["updated_at"],
    )


def test_signer_failures_and_exports_never_persist_key_material(tmp_path: Path) -> None:
    database, subject_id, _wallets, economy, network, asset, source = _fixture(tmp_path)
    goal_id = _goal(database, subject_id)
    bounty = _bounty(
        economy,
        subject_id,
        network.network_id,
        asset.asset_id,
        goal_id,
        amount="2",
        max_submissions=1,
        reward_slots=1,
        key="signer-isolation",
    )
    economy.publish_bounty(bounty.bounty_id, subject_id, actor="operator")
    submission = economy.submit(bounty.bounty_id, subject_id, _submission(99))
    accepted = economy.decide_submission(
        submission.submission_id,
        subject_id,
        accepted=True,
        reason="verified",
        actor="operator",
    )
    order = economy.order_for_submission(accepted.submission_id, subject_id)
    assert order is not None

    class ExplodingSigner(MockSigner):
        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            del transfer, request_id
            raise RuntimeError("private-key-material-must-not-leak")

    engine = WalletPaymentExecutionEngine(
        database,
        ExplodingSigner(chain_id=network.chain_id, source_address=source.address),
        economy=economy,
    )
    record = engine.execute_order(order.order_id, subject_id, actor="operator")
    assert record.status == "unknown"
    assert record.error_code == "signer_transport_unknown"
    artifact = RuntimeLogExporter(database).export(subject_id, actor="operator")
    payload = artifact.content.lower()
    assert b"private-key-material-must-not-leak" not in payload
    assert b"private_key" not in payload
