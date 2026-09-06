from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import noyra.wallet.store as wallet_store_module
from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.wallet import (
    WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS,
    WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
    WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS,
    WalletAddressInput,
    WalletAddressRecord,
    WalletAssetInput,
    WalletAssetRecord,
    WalletBalanceSnapshotInput,
    WalletBalanceSnapshotRecord,
    WalletNetworkInput,
    WalletNetworkRecord,
    WalletStore,
)
from noyra.wallet.types import canonical_timestamp

EVALUATED_AT = "2026-08-31T00:00:00.000+00:00"
STALE_AFTER = WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS


@pytest.fixture
def observation_store(tmp_path: Path) -> tuple[Database, str, WalletStore]:
    database = Database(tmp_path / "observation-health.sqlite3")
    subject_id = "Noyra-observation-health"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id, WalletStore(database)


@pytest.fixture
def observation_server(tmp_path: Path) -> Iterator[NoyraHTTPServer]:
    """Build an unstarted HTTP server for direct fail-closed projection tests."""

    data_dir = tmp_path / "service-data"
    subject_id = "Noyra-observation-health-service"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"seed": subject_id}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("observation-health-admin-token-with-sufficient-entropy"),
        active_interval_seconds=1,
        sleep_interval_seconds=1,
        error_backoff_seconds=1,
    )
    kernel = SubjectKernel(data_dir / "noyra.sqlite3", subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    try:
        yield server
    finally:
        server.close()
        kernel.close()


def _network(
    store: WalletStore,
    subject_id: str,
    *,
    label: str = "Health Network",
    chain_id: int = 9001,
    native_symbol: str = "HETH",
) -> WalletNetworkRecord:
    return store.register_network(
        subject_id,
        WalletNetworkInput(
            label=label,
            chain_id=chain_id,
            native_symbol=native_symbol,
            rpc_url="https://rpc.example",
        ),
        actor="operator",
    )


def _asset(
    store: WalletStore,
    subject_id: str,
    network_id: str,
    *,
    name: str = "Health Ether",
    symbol: str = "HETH",
) -> WalletAssetRecord:
    return store.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network_id,
            asset_type="native",
            name=name,
            symbol=symbol,
            decimals=18,
        ),
        actor="operator",
    )


def _address(
    store: WalletStore, subject_id: str, network_id: str, index: int
) -> WalletAddressRecord:
    return store.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network_id,
            label=f"Health address {index}",
            address=f"0x{index:040x}",
            purpose="observation",
        ),
        actor="operator",
    )


def _graph(
    store: WalletStore,
    subject_id: str,
    *,
    label: str = "Health Network",
    chain_id: int = 9001,
    native_symbol: str = "HETH",
    address_index: int = 1,
) -> tuple[WalletNetworkRecord, WalletAssetRecord, WalletAddressRecord]:
    network = _network(
        store,
        subject_id,
        label=label,
        chain_id=chain_id,
        native_symbol=native_symbol,
    )
    asset = _asset(store, subject_id, network.network_id, symbol=native_symbol)
    address = _address(store, subject_id, network.network_id, address_index)
    return network, asset, address


def _time_with_age(age_seconds: int, *, reference: str = EVALUATED_AT) -> str:
    parsed = datetime.fromisoformat(reference)
    return canonical_timestamp((parsed - timedelta(seconds=age_seconds)).isoformat())


def _time_after(seconds: int, *, reference: str = EVALUATED_AT) -> str:
    parsed = datetime.fromisoformat(reference)
    return canonical_timestamp((parsed + timedelta(seconds=seconds)).isoformat())


def _record(
    store: WalletStore,
    subject_id: str,
    asset_id: str,
    address_id: str,
    balance: str,
    observed_at: str,
) -> WalletBalanceSnapshotRecord:
    return store.record_balance_snapshot(
        subject_id,
        WalletBalanceSnapshotInput(
            asset_id=asset_id,
            address_id=address_id,
            balance=balance,
            observed_at=observed_at,
        ),
        actor="operator",
    )


def _assert_health_shape(health: dict[str, Any], *, evaluated_at: str = EVALUATED_AT) -> None:
    assert set(health) == {
        "status",
        "evaluated_at",
        "stale_after_seconds",
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
        "max_age_seconds",
    }
    assert health["status"] in {"ok", "attention", "degraded"}
    assert health["evaluated_at"] == evaluated_at
    assert health["stale_after_seconds"] == STALE_AFTER
    for field in (
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
    ):
        assert type(health[field]) is int
        assert health[field] >= 0
    assert health["max_age_seconds"] is None or (
        type(health["max_age_seconds"]) is int and health["max_age_seconds"] >= 0
    )
    assert (
        health["fresh_pairs"]
        + health["near_expiry_pairs"]
        + health["stale_pairs"]
        + health["future_pairs"]
        == health["observed_pairs"]
    )


def test_observation_health_empty_history_and_never_pair_are_distinct(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store

    empty = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(empty)
    assert empty == {
        "status": "ok",
        "evaluated_at": EVALUATED_AT,
        "stale_after_seconds": STALE_AFTER,
        "observed_pairs": 0,
        "fresh_pairs": 0,
        "near_expiry_pairs": 0,
        "stale_pairs": 0,
        "never_pairs": 0,
        "future_pairs": 0,
        "anomalous_pairs": 0,
        "max_age_seconds": None,
    }

    _network_record, _asset_record, _address_record = _graph(store, subject_id)
    never = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(never)
    assert never["status"] == "attention"
    assert never["observed_pairs"] == 0
    assert never["never_pairs"] == 1
    assert never["stale_pairs"] == 0
    assert never["max_age_seconds"] is None


def test_observation_health_classifies_fresh_near_boundary_stale_future_and_never(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    network, asset, _first_address = _graph(store, subject_id)
    addresses = [_first_address] + [
        _address(store, subject_id, network.network_id, index) for index in range(2, 7)
    ]

    # Ages are deliberately on both sides of the documented 75% near-expiry
    # boundary and the strict stale boundary.  The sixth pair is left empty.
    ages = (3_600, 64_800, STALE_AFTER, STALE_AFTER + 1)
    for address, age, balance in zip(addresses[:4], ages, ("1", "2", "3", "4"), strict=True):
        _record(
            store,
            subject_id,
            asset.asset_id,
            address.address_id,
            balance,
            _time_with_age(age),
        )
    _record(
        store,
        subject_id,
        asset.asset_id,
        addresses[4].address_id,
        "5",
        _time_after(3_600),
    )

    health = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(health)
    assert health["status"] == "attention"
    assert health["observed_pairs"] == 5
    assert health["fresh_pairs"] == 1
    assert health["near_expiry_pairs"] == 2  # 75% and exactly the stale boundary
    assert health["stale_pairs"] == 1  # strictly older than the threshold
    assert health["never_pairs"] == 1
    assert health["future_pairs"] == 1
    assert health["anomalous_pairs"] == 0
    assert health["max_age_seconds"] == STALE_AFTER + 1


def test_observation_health_detects_adjacent_jump_but_not_normal_change(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    network, asset, normal_address = _graph(store, subject_id, address_index=10)
    jump_address = _address(store, subject_id, network.network_id, 11)

    normal_times = (_time_with_age(300), _time_with_age(200), _time_with_age(100))
    for balance, observed_at in zip(("100", "101", "102"), normal_times, strict=True):
        _record(store, subject_id, asset.asset_id, normal_address.address_id, balance, observed_at)

    jump_times = (_time_with_age(300), _time_with_age(200), _time_with_age(100))
    for balance, observed_at in zip(("1", "2", "9" * 200), jump_times, strict=True):
        _record(store, subject_id, asset.asset_id, jump_address.address_id, balance, observed_at)

    health = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(health)
    assert health["observed_pairs"] == 2
    assert health["fresh_pairs"] == 2
    assert health["near_expiry_pairs"] == 0
    assert health["stale_pairs"] == 0
    assert health["never_pairs"] == 0
    assert health["anomalous_pairs"] == 1
    assert health["status"] == "attention"


def test_observation_health_same_timestamp_conflict_is_anomalous_and_latest_tie_breaks(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    _network_record, asset, address = _graph(store, subject_id, address_index=20)
    observed_at = _time_with_age(60)
    first = _record(store, subject_id, asset.asset_id, address.address_id, "10", observed_at)
    second = _record(store, subject_id, asset.asset_id, address.address_id, "20", observed_at)

    snapshots = store.list_balance_snapshots(subject_id, asset_id=asset.asset_id)
    assert {snapshot.snapshot_id for snapshot in snapshots} == {
        first.snapshot_id,
        second.snapshot_id,
    }
    expected_latest = max(snapshots, key=lambda snapshot: snapshot.snapshot_id)
    latest = store.latest_balances(subject_id, asset_id=asset.asset_id)
    assert len(latest) == 1
    assert latest[0].snapshot_id == expected_latest.snapshot_id
    assert latest[0].balance == expected_latest.balance

    health = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(health)
    assert health["observed_pairs"] == 1
    assert health["fresh_pairs"] == 1
    assert health["anomalous_pairs"] == 1
    assert health["status"] == "attention"


def test_observation_health_isolates_networks_subjects_and_revoked_targets(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    network_a, asset_a, address_a = _graph(
        store,
        subject_id,
        label="Health Network A",
        chain_id=9101,
        native_symbol="HAA",
        address_index=30,
    )
    network_b, asset_b, address_b = _graph(
        store,
        subject_id,
        label="Health Network B",
        chain_id=9102,
        native_symbol="HBB",
        address_index=31,
    )
    _record(store, subject_id, asset_a.asset_id, address_a.address_id, "1", _time_with_age(60))
    _record(
        store,
        subject_id,
        asset_b.asset_id,
        address_b.address_id,
        "2",
        _time_with_age(STALE_AFTER + 1),
    )

    # A second subject has its own graph and history; it must not affect the
    # first subject's counts even though both are stored in one SQLite file.
    other_subject = "Noyra-observation-health-other"
    IdentityStore(database).ensure(other_subject, content_hash({"subject": other_subject}))
    _other_network, other_asset, other_address = _graph(
        store,
        other_subject,
        label="Other Health Network",
        chain_id=9201,
        native_symbol="OTH",
        address_index=32,
    )
    _record(
        store,
        other_subject,
        other_asset.asset_id,
        other_address.address_id,
        "999",
        _time_with_age(STALE_AFTER + 1),
    )

    health = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(health)
    assert health["observed_pairs"] == 2
    assert health["fresh_pairs"] == 1
    assert health["stale_pairs"] == 1
    assert health["never_pairs"] == 0
    assert health["max_age_seconds"] == STALE_AFTER + 1

    # Revoked resources and their immutable historical snapshots leave no
    # active targets and therefore no observed/never pair in the projection.
    store.revoke_asset(
        asset_a.asset_id, reason="retire asset", actor="operator", subject_id=subject_id
    )
    store.revoke_address(
        address_a.address_id, reason="retire address", actor="operator", subject_id=subject_id
    )
    store.revoke_asset(
        asset_b.asset_id, reason="retire asset", actor="operator", subject_id=subject_id
    )
    store.revoke_address(
        address_b.address_id, reason="retire address", actor="operator", subject_id=subject_id
    )
    store.revoke_network(
        network_a.network_id, reason="retire network", actor="operator", subject_id=subject_id
    )
    store.revoke_network(
        network_b.network_id, reason="retire network", actor="operator", subject_id=subject_id
    )

    revoked = store.observation_health(subject_id, now=EVALUATED_AT)
    _assert_health_shape(revoked)
    assert revoked["status"] == "ok"
    assert revoked["observed_pairs"] == 0
    assert revoked["never_pairs"] == 0
    assert revoked["fresh_pairs"] == 0
    assert revoked["stale_pairs"] == 0
    assert revoked["anomalous_pairs"] == 0


def test_observation_health_rejects_invalid_reference_and_age_window(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    invalid_references: tuple[object, ...] = (
        "2026-08-31T00:00:00Z",  # non-canonical spelling
        "2026-08-31T00:00:00.123",  # naive
        datetime(2026, 8, 31, tzinfo=UTC, microsecond=1),  # sub-millisecond
        123,
        True,
    )
    for invalid in invalid_references:
        with pytest.raises((TypeError, ValueError), match=r"reference|timezone|canonical|invalid"):
            store.observation_health(subject_id, now=invalid)  # type: ignore[arg-type]

    for invalid_age in (-1, WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS + 1, True, "86400"):
        with pytest.raises(ValueError, match="age window"):
            store.observation_health(subject_id, max_age_seconds=invalid_age)  # type: ignore[arg-type]


def test_observation_health_zero_window_keeps_exact_time_fresh(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    _network_record, asset, address = _graph(store, subject_id, address_index=41)
    _record(store, subject_id, asset.asset_id, address.address_id, "7", EVALUATED_AT)

    health = store.observation_health(
        subject_id,
        max_age_seconds=0,
        now=EVALUATED_AT,
    )
    assert health["status"] == "ok"
    assert health["stale_after_seconds"] == 0
    assert health["fresh_pairs"] == 1
    assert health["near_expiry_pairs"] == 0
    assert health["stale_pairs"] == 0


def test_observation_health_uses_fractional_near_expiry_boundary(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    network, asset, first_address = _graph(store, subject_id, address_index=42)
    second_address = _address(store, subject_id, network.network_id, 43)
    # With a three-second window, 75% is 2.25 seconds.  Millisecond
    # precision lets the boundary be tested without rounding it up to three
    # seconds (which would incorrectly classify 2.5 seconds as fresh).
    _record(
        store,
        subject_id,
        asset.asset_id,
        first_address.address_id,
        "1",
        _time_with_age(2, reference=EVALUATED_AT),
    )
    half_second_after_cutoff = canonical_timestamp(
        (datetime.fromisoformat(EVALUATED_AT) - timedelta(seconds=2.5)).isoformat()
    )
    _record(
        store,
        subject_id,
        asset.asset_id,
        second_address.address_id,
        "2",
        half_second_after_cutoff,
    )

    health = store.observation_health(
        subject_id,
        max_age_seconds=3,
        now=EVALUATED_AT,
    )
    assert health["observed_pairs"] == 2
    assert health["fresh_pairs"] == 1
    assert health["near_expiry_pairs"] == 1
    assert health["stale_pairs"] == 0


def test_observation_health_rejects_noncanonical_persisted_timestamp(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    _network_record, asset, address = _graph(store, subject_id, address_index=40)
    # The append-only table trigger does not validate timestamp syntax.  Insert
    # a deliberately damaged durable row so the read path, not the input model,
    # proves it fails closed with IntegrityError.
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO wallet_balance_snapshots(
                snapshot_id, subject_id, network_id, asset_id, address_id,
                balance, source, observed_at, created_at, state_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "walletbal_invalid_timestamp",
                subject_id,
                _network_record.network_id,
                asset.asset_id,
                address.address_id,
                "7",
                "operator_observation",
                "2026-08-31T00:00:00Z",
                EVALUATED_AT,
                "0" * 64,
            ),
        )
    with pytest.raises(IntegrityError, match="wallet balance snapshot"):
        store.observation_health(subject_id, now=EVALUATED_AT)


def test_observation_health_rejects_noncanonical_persisted_creation_time(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    network, asset, address = _graph(store, subject_id, address_index=45)
    snapshot_id = "walletbal_noncanonical_created_at"
    observed_at = EVALUATED_AT
    created_at = "2026-08-31T00:00:00.000Z"
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO wallet_balance_snapshots(
                snapshot_id, subject_id, network_id, asset_id, address_id,
                balance, source, observed_at, created_at, state_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                subject_id,
                network.network_id,
                asset.asset_id,
                address.address_id,
                "7",
                "operator_observation",
                observed_at,
                created_at,
                WalletStore._balance_hash(
                    snapshot_id,
                    subject_id,
                    network.network_id,
                    asset.asset_id,
                    address.address_id,
                    "7",
                    "operator_observation",
                    observed_at,
                    created_at,
                ),
            ),
        )
    with pytest.raises(IntegrityError, match="wallet balance snapshot time is invalid"):
        store.observation_health(subject_id, now=EVALUATED_AT)


def test_observation_health_rejects_mismatched_snapshot_relationships(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    _network_record, asset, address = _graph(store, subject_id, address_index=46)
    unrelated_network = _network(
        store,
        subject_id,
        label="Unrelated health network",
        chain_id=9002,
    )
    snapshot_id = "walletbal_mismatched_relationship"
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER validate_wallet_balance_snapshot_insert")
        connection.execute(
            """INSERT INTO wallet_balance_snapshots(
                snapshot_id, subject_id, network_id, asset_id, address_id,
                balance, source, observed_at, created_at, state_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                subject_id,
                unrelated_network.network_id,
                asset.asset_id,
                address.address_id,
                "7",
                "operator_observation",
                EVALUATED_AT,
                EVALUATED_AT,
                WalletStore._balance_hash(
                    snapshot_id,
                    subject_id,
                    unrelated_network.network_id,
                    asset.asset_id,
                    address.address_id,
                    "7",
                    "operator_observation",
                    EVALUATED_AT,
                    EVALUATED_AT,
                ),
            ),
        )
    with pytest.raises(IntegrityError, match="wallet observation reference mismatch"):
        store.observation_health(subject_id, now=EVALUATED_AT)


def test_observation_health_is_consistent_during_concurrent_appends(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    _database, subject_id, store = observation_store
    _network, asset, address = _graph(store, subject_id, address_index=51)
    errors: list[BaseException] = []

    def append_history() -> None:
        try:
            for index in range(20):
                _record(
                    store,
                    subject_id,
                    asset.asset_id,
                    address.address_id,
                    str(index + 1),
                    _time_with_age(index + 10),
                )
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    writer = threading.Thread(target=append_history)
    writer.start()
    for _ in range(20):
        health = store.observation_health(subject_id, now=EVALUATED_AT)
        _assert_health_shape(health)
        observed_pairs = health["observed_pairs"]
        never_pairs = health["never_pairs"]
        assert isinstance(observed_pairs, int) and observed_pairs in {0, 1}
        assert isinstance(never_pairs, int) and never_pairs in {0, 1}
        assert observed_pairs + never_pairs == 1
    writer.join(timeout=10)
    assert not writer.is_alive()
    assert errors == []


def test_status_summary_keeps_history_and_health_on_one_sqlite_snapshot(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    _network, asset, address = _graph(store, subject_id, address_index=53)
    start_writer = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def append_after_summary_count() -> None:
        start_writer.wait(timeout=10)
        try:
            _record(
                store,
                subject_id,
                asset.asset_id,
                address.address_id,
                "99",
                _time_with_age(1),
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            writer_errors.append(error)
        finally:
            writer_done.set()

    writer = threading.Thread(target=append_after_summary_count)
    writer.start()

    class InterleavingConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection
            self._writer_triggered = False

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            result = self._connection.execute(sql, *args, **kwargs)
            normalized = " ".join(sql.lower().split())
            if (
                not self._writer_triggered
                and "select count(*) as count" in normalized
                and "from wallet_balance_snapshots" in normalized
            ):
                self._writer_triggered = True
                start_writer.set()
                assert writer_done.wait(timeout=10)
            return result

    class InterleavingDatabase(Database):
        @contextmanager
        def read_transaction(self) -> Iterator[Any]:
            with super().read_transaction() as connection:
                yield InterleavingConnection(connection)

    try:
        summary_store = WalletStore(InterleavingDatabase(database.path, initialize=False))
        summary = summary_store.status_summary(subject_id)
    finally:
        writer.join(timeout=10)

    assert not writer.is_alive()
    assert writer_errors == []
    history = summary["balance_history"]
    assert history["snapshots"] == 0
    assert history["latest_observed_at"] is None
    observation = history["observation_health"]
    assert observation["observed_pairs"] == 0
    assert observation["never_pairs"] == 1

    # The append committed after the summary's read transaction began and is
    # visible to a later read, proving the summary intentionally used the
    # earlier single snapshot for both projections.
    later = store.observation_health(subject_id, now=EVALUATED_AT)
    assert later["observed_pairs"] == 1


def test_observation_health_consumes_history_incrementally_without_fetchall(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    _network, asset, address = _graph(store, subject_id, address_index=50)
    for index in range(4):
        _record(
            store,
            subject_id,
            asset.asset_id,
            address.address_id,
            str(index + 1),
            _time_with_age(index + 1),
        )

    class NoFetchallCursor:
        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        def __iter__(self) -> Iterator[Any]:
            return iter(self._cursor)

        def fetchone(self) -> Any:
            return self._cursor.fetchone()

        def fetchall(self) -> list[Any]:
            raise AssertionError("observation health must consume cursors incrementally")

    class NoFetchallConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def execute(self, *args: Any, **kwargs: Any) -> NoFetchallCursor:
            return NoFetchallCursor(self._connection.execute(*args, **kwargs))

    class NoFetchallDatabase(Database):
        @contextmanager
        def read_transaction(self) -> Iterator[Any]:
            with super().read_transaction() as connection:
                yield NoFetchallConnection(connection)

    wrapped = WalletStore(NoFetchallDatabase(database.path, initialize=False))
    health = wrapped.observation_health(subject_id, now=EVALUATED_AT)
    assert health["observed_pairs"] == 1
    assert health["anomalous_pairs"] == 0  # 1 -> 2 -> 3 -> 4 are normal
    assert health["status"] == "ok"


def test_observation_health_bounds_active_pair_registry(
    observation_store: tuple[Database, str, WalletStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, subject_id, store = observation_store
    network, _asset_record, _address_record = _graph(store, subject_id, address_index=60)
    _address(store, subject_id, network.network_id, 61)
    monkeypatch.setattr(
        wallet_store_module,
        "WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS",
        1,
    )

    with pytest.raises(IntegrityError, match="wallet observation active pair limit is exceeded"):
        store.observation_health(subject_id, now=EVALUATED_AT)


def test_observation_health_history_query_uses_bounded_subject_index_plan(
    observation_store: tuple[Database, str, WalletStore],
) -> None:
    database, subject_id, store = observation_store
    _network, asset, address = _graph(store, subject_id, address_index=52)
    _record(store, subject_id, asset.asset_id, address.address_id, "1", _time_with_age(1))
    plan_details: list[str] = []

    class PlanConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
            normalized = " ".join(sql.lower().split())
            if (
                "from wallet_balance_snapshots s" in normalized
                and "order by s.network_id" in normalized
            ):
                for row in self._connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, parameters
                ).fetchall():
                    plan_details.append(str(row["detail"]))
            return self._connection.execute(sql, parameters)

    class PlanDatabase(Database):
        @contextmanager
        def read_transaction(self) -> Iterator[Any]:
            with super().read_transaction() as connection:
                yield PlanConnection(connection)

    health = WalletStore(PlanDatabase(database.path, initialize=False)).observation_health(
        subject_id, now=EVALUATED_AT
    )
    assert health["observed_pairs"] == 1
    assert plan_details
    combined = " ".join(plan_details).upper()
    assert "IDX_WALLET_BALANCE_SNAPSHOTS_SUBJECT_LOOKUP" in combined
    assert "USE TEMP B-TREE" not in combined


def test_status_summary_embeds_observation_health_and_service_fails_closed(
    observation_server: NoyraHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = observation_server.kernel.subject_id
    summary = observation_server.wallets.status_summary(subject_id)
    history = summary["balance_history"]
    assert isinstance(history, dict)
    assert "observation_health" in history
    _assert_health_shape(
        history["observation_health"], evaluated_at=history["observation_health"]["evaluated_at"]
    )

    malformed = dict(summary)
    malformed_history = dict(history)
    malformed_history.pop("observation_health")
    malformed["balance_history"] = malformed_history
    monkeypatch.setattr(observation_server.wallets, "status_summary", lambda _subject: malformed)
    assert observation_server.wallet_health() == {"status": "degraded", "reason": "unavailable"}


def test_service_health_does_not_fail_open_or_leak_malformed_observation_data(
    observation_server: NoyraHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = observation_server.kernel.subject_id
    summary = observation_server.wallets.status_summary(subject_id)
    history = dict(summary["balance_history"])
    valid = dict(history["observation_health"])
    valid["rpc_url"] = "https://should-not-leak.example"
    malformed = dict(summary)
    malformed["balance_history"] = {**history, "observation_health": valid}
    monkeypatch.setattr(observation_server.wallets, "status_summary", lambda _subject: malformed)

    result = observation_server.wallet_health()
    assert result == {"status": "degraded", "reason": "unavailable"}
    assert "should-not-leak" not in json.dumps(result, sort_keys=True)


def test_service_health_rejects_inconsistent_wallet_diagnostic_counts(
    observation_server: NoyraHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = observation_server.kernel.subject_id
    summary = observation_server.wallets.status_summary(subject_id)
    valid_summary = summary
    history = dict(summary["balance_history"])
    observation = dict(history["observation_health"])
    observation["observed_pairs"] = 1
    observation["fresh_pairs"] = 1
    observation["anomalous_pairs"] = 2
    observation["max_age_seconds"] = 0
    history["observation_health"] = observation
    malformed_summary = {**summary, "balance_history": history}
    monkeypatch.setattr(
        observation_server.wallets,
        "status_summary",
        lambda _subject: malformed_summary,
    )
    assert observation_server.wallet_health() == {"status": "degraded", "reason": "unavailable"}

    malformed_acquisition = dict(observation_server.wallet_acquisitions.status_summary(subject_id))
    malformed_acquisition["expired_running"] = 1
    monkeypatch.setattr(
        observation_server.wallets,
        "status_summary",
        lambda _subject: valid_summary,
    )
    monkeypatch.setattr(
        observation_server.wallet_acquisitions,
        "status_summary",
        lambda _subject: malformed_acquisition,
    )
    result = observation_server.wallet_health()
    assert result["status"] == "degraded"
    assert result["acquisition"] == {"status": "degraded", "reason": "unavailable"}


def test_service_health_propagates_observation_degraded_and_acquisition_unavailable(
    observation_server: NoyraHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = observation_server.kernel.subject_id
    summary = observation_server.wallets.status_summary(subject_id)
    history = dict(summary["balance_history"])
    observation = dict(history["observation_health"])
    observation["status"] = "degraded"
    observation["never_pairs"] = 0
    observation["fresh_pairs"] = 0
    observation["near_expiry_pairs"] = 0
    observation["stale_pairs"] = 0
    observation["future_pairs"] = 0
    observation["anomalous_pairs"] = 0
    history["observation_health"] = observation
    summary = {**summary, "balance_history": history}
    monkeypatch.setattr(observation_server.wallets, "status_summary", lambda _subject: summary)
    monkeypatch.setattr(
        observation_server.wallet_acquisitions,
        "status_summary",
        lambda _subject: {"unexpected": "shape"},
    )

    result = observation_server.wallet_health()
    assert result["status"] == "degraded"
    assert result["balance_history"]["observation_health"]["status"] == "degraded"
    assert result["acquisition"] == {"status": "degraded", "reason": "unavailable"}


# Keep a tiny explicit check that the default remains the documented one-day
# window; this catches accidental drift while allowing callers to configure a
# smaller bounded window.
def test_observation_health_default_window_is_bounded() -> None:
    assert STALE_AFTER == 86_400
    assert WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS == 100_000
    assert WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS == 2_592_000
