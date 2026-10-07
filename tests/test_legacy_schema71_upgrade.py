from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.request import urlopen

import pytest

from noyra.core import Database, IntegrityRegistry
from noyra.core.database import (
    _SCHEMA_DDL_FINGERPRINTS,
    _SCHEMA_STRUCTURE_FINGERPRINTS,
    CURRENT_SCHEMA_VERSION,
)
from noyra.core.errors import IntegrityError
from noyra.core.evidence_schema import TABLES_V80
from noyra.core.provider_health import ProviderHealthStore
from noyra.model.ledger import ModelLedger
from noyra.service import NoyraService, ServiceSettings
from noyra.wallet.economy import WalletEconomyStore
from support.historical import (
    HistoricalFixture,
    load_historical_fixtures,
    materialize_historical_database,
)

FIXTURES = tuple(
    fixture
    for fixture in load_historical_fixtures(
        Path(__file__).parent / "fixtures" / "historical" / "manifest.json"
    )
    if fixture.schema_version == 71
)


@pytest.fixture(params=FIXTURES, ids=lambda item: item.name)
def fixture(request: pytest.FixtureRequest) -> HistoricalFixture:
    assert isinstance(request.param, HistoricalFixture)
    return request.param


def snapshot(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def test_github_schema71_upgrade_preserves_history_and_passes_integrity(
    tmp_path: Path, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("subject_identity", "events", "model_calls", "provider_health_state")
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(provider_health_buckets)")
        }
        assert "unknown_count" not in columns
        connection.row_factory = sqlite3.Row
        policies = [
            dict(row) for row in connection.execute("SELECT * FROM wallet_payment_policies")
        ]
        health_before = [
            dict(row) for row in connection.execute("SELECT * FROM provider_health_buckets")
        ]

    database = Database(path)
    with database.connection() as connection:
        for table, rows in before.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ] == rows
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        assert (
            database.schema_ddl_fingerprint(connection)
            == _SCHEMA_DDL_FINGERPRINTS[CURRENT_SCHEMA_VERSION]
        )
        assert (
            database.schema_structure_fingerprint(connection)
            == _SCHEMA_STRUCTURE_FINGERPRINTS[CURRENT_SCHEMA_VERSION]
        )
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        counts = {
            row["table_name"]: row["row_count"]
            for row in connection.execute(
                "SELECT table_name,row_count FROM evidence_row_counts WHERE subject_id=?",
                (fixture.subject_id,),
            )
        }
        assert counts == {
            table: connection.execute(
                f"SELECT count(*) FROM {table} WHERE subject_id=?", (fixture.subject_id,)
            ).fetchone()[0]
            for table in TABLES_V80
        }
        buckets = [
            tuple(row) for row in connection.execute("SELECT * FROM provider_health_buckets")
        ]
        assert [
            dict(row) for row in connection.execute("SELECT * FROM wallet_payment_policies")
        ] == policies
        for old, new in zip(
            health_before, connection.execute("SELECT * FROM provider_health_buckets"), strict=True
        ):
            assert {key: new[key] for key in old if key != "state_hash"} == {
                key: value for key, value in old.items() if key != "state_hash"
            }
            assert new["unknown_count"] == 0
            assert new["state_hash"] != old["state_hash"]

    health = ProviderHealthStore(database).list_projection(fixture.subject_id, "model")
    assert len(health) == 1
    assert health[0]["attempt_count"] == 2
    assert health[0]["success_count"] == health[0]["failure_count"] == 1
    assert health[0]["unknown_count"] == 0
    assert health[0]["error_counts"] == {"timeout": 1}
    report = IntegrityRegistry().run(
        database,
        fixture.subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("core.schema_contract", "operations.provider_health"),
    )
    assert report.p0 == ()
    ledger = ModelLedger(database)
    call, created = ledger.prepare_call(
        fixture.subject_id,
        "historical-provider",
        "historical-model",
        "fixture",
        "a" * 64,
        "historical-0",
    )
    assert not created and call.status == "failed"
    policy = WalletEconomyStore(database).get_policy(fixture.subject_id)
    assert policy.mode == "conditional_confirmation"
    assert policy.recipient_allowlist_enabled
    assert policy.allowed_recipient_addresses == ("0x" + "1" * 40,)
    assert policy.emergency_paused and not policy.automation_enabled

    reopened = Database(path)
    with reopened.connection() as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM provider_health_buckets")
        ] == buckets
    assert not list(tmp_path.glob("*.pre-migration-*.bak"))


def test_failed_schema71_upgrade_restores_the_original_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    before = snapshot(path)
    original = Database._execute_sql_script

    def fail_counter_migration(connection: sqlite3.Connection, script: str) -> None:
        original(connection, script)
        if "CREATE TABLE IF NOT EXISTS evidence_row_counts" in script:
            raise RuntimeError("injected v80 failure after provider migration")

    with monkeypatch.context() as context:
        context.setattr(Database, "_execute_sql_script", staticmethod(fail_counter_migration))
        with pytest.raises(RuntimeError, match="injected v80 failure"):
            Database(path)
    assert snapshot(path) == before
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "71"
        )
        assert "unknown_count" not in {
            row[1] for row in connection.execute("PRAGMA table_info(provider_health_buckets)")
        }
    Database(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE provider_health_buckets SET latency_total_ms=latency_total_ms+1",
        "UPDATE provider_health_buckets SET error_counts_json='{\"timeout\":2}'",
        "UPDATE provider_health_buckets SET state_hash='" + "0" * 64 + "'",
        "ALTER TABLE provider_health_buckets ADD COLUMN unexpected TEXT",
        "ALTER TABLE wallet_payment_policies ADD COLUMN unexpected TEXT",
        "CREATE INDEX unexpected_wallet_policy_index ON wallet_payment_policies(mode)",
        "CREATE TRIGGER unexpected_wallet_policy_trigger AFTER UPDATE ON wallet_payment_policies "
        "BEGIN SELECT 1; END",
    ],
)
def test_schema71_upgrade_rejects_tampered_health_data_or_structure(
    tmp_path: Path, mutation: str, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(mutation)
        connection.commit()
    before = snapshot(path)
    with pytest.raises(
        (IntegrityError, RuntimeError), match=r"integrity|fingerprint|contract|dependent objects"
    ):
        Database(path)
    assert snapshot(path) == before


def test_current_health_buckets_survive_a_replayed_v80_migration(
    tmp_path: Path, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    database = Database(path)
    ProviderHealthStore(database).record_attempt(
        fixture.subject_id,
        "model",
        "new-provider",
        "unknown-attempt",
        False,
        30,
        "timeout",
        outcome_unknown=True,
    )
    with database.transaction() as connection:
        buckets = [
            tuple(row) for row in connection.execute("SELECT * FROM provider_health_buckets")
        ]
        connection.execute("UPDATE schema_meta SET value='79' WHERE key='schema_version'")
    upgraded = Database(path)
    with upgraded.connection() as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM provider_health_buckets")
        ] == buckets


def test_server_upgrade_failure_fingerprint_is_reproduced_before_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = next(item for item in FIXTURES if item.name == "schema-v71-from-v63")
    path = materialize_historical_database(fixture, tmp_path)
    for name in (
        "_upgrade_provider_health_metrics",
        "_upgrade_provider_health_bucket_hashes",
        "_upgrade_wallet_payment_policy_layout",
    ):
        monkeypatch.setattr(Database, name, staticmethod(lambda connection: None))
    with pytest.raises(
        RuntimeError,
        match="found f20b36b9f492146624f4795dc10a9184a42b45365cb0fa17c2a1d16dfc969937",
    ):
        Database(path)


def test_service_can_start_and_become_ready_after_schema71_upgrade(
    tmp_path: Path, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    path.rename(tmp_path / "noyra.sqlite3")
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path,
            subject_id=fixture.subject_id,
            genesis_hash=hashlib.sha256(fixture.subject_id.encode()).hexdigest(),
            host="127.0.0.1",
            port=0,
            integrity_mode="pause",
            integrity_startup_deadline_seconds=120,
        )
    )
    try:
        service.boot()
        report = service.integrity.latest_report
        assert report is not None and report.status == "ok"
        assert service.kernel.lifecycle.current().state == "active"
        service.http.start()
        port = service.http.server.server_address[1]
        for endpoint in ("live", "ready"):
            with urlopen(f"http://127.0.0.1:{port}/health/{endpoint}", timeout=10) as response:
                assert response.status == 200
    finally:
        service.close()
