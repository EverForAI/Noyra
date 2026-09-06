from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.integrity import IntegrityAuditLimits, IntegrityRegistry
from support.faults import (
    DeterministicGate,
    FaultInjector,
    FaultRule,
    InjectedFault,
    StateMachineProbe,
    hold_sqlite_write_lock,
)
from support.historical import (
    HistoricalFixture,
    load_historical_fixtures,
    materialize_historical_database,
)
from support.synthetic import load_synthetic_profiles, populate_synthetic_history

TEST_ROOT = Path(__file__).resolve().parent
HISTORICAL_MANIFEST = TEST_ROOT / "fixtures" / "historical" / "manifest.json"
SYNTHETIC_PROFILES = TEST_ROOT / "fixtures" / "synthetic" / "profiles.json"
ACCEPTANCE_MATRIX = TEST_ROOT / "contracts" / "remediation_acceptance.json"


HISTORICAL_FIXTURES = load_historical_fixtures(HISTORICAL_MANIFEST)
MIGRATABLE_FIXTURES = tuple(
    fixture for fixture in HISTORICAL_FIXTURES if fixture.expected_migration_error is None
)


@pytest.mark.parametrize("fixture", MIGRATABLE_FIXTURES, ids=lambda item: item.name)
def test_historical_database_migrates_without_losing_anchor(
    tmp_path: Path, fixture: HistoricalFixture
) -> None:
    database_path = materialize_historical_database(fixture, tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        before_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        before_event = connection.execute(
            "SELECT subject_id, payload_json, payload_hash FROM events WHERE event_id = ?",
            (fixture.anchor_event_id,),
        ).fetchone()
    assert before_version == fixture.schema_version
    assert before_event is not None

    database = Database(database_path)
    with database.connection() as connection:
        after_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        after_event = connection.execute(
            "SELECT subject_id, payload_json, payload_hash FROM events WHERE event_id = ?",
            (fixture.anchor_event_id,),
        ).fetchone()
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert after_version == CURRENT_SCHEMA_VERSION
    assert after_event is not None
    assert tuple(after_event) == tuple(before_event)
    assert after_event[0] == fixture.subject_id
    assert quick_check == "ok"
    assert foreign_key_errors == []
    assert database_path.is_relative_to(tmp_path)
    backup_path = database_path.with_name(
        f"{database_path.name}.pre-migration-v{fixture.schema_version}.bak"
    )
    assert not backup_path.exists()


def test_historical_fixture_manifest_has_no_open_migration_gaps() -> None:
    assert [
        fixture.name for fixture in HISTORICAL_FIXTURES if fixture.expected_migration_error
    ] == []


def test_future_schema_is_rejected_before_any_write(tmp_path: Path) -> None:
    database_path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', ?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('must-survive')")
        connection.commit()
    before_bytes = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="newer than runtime"):
        Database(database_path)

    assert database_path.read_bytes() == before_bytes
    with closing(sqlite3.connect(database_path)) as connection:
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]
    assert int(marker) == CURRENT_SCHEMA_VERSION + 1
    assert sentinel == "must-survive"
    assert not list(database_path.parent.glob(f"{database_path.name}.pre-migration-*.bak"))


def test_failed_migration_restores_verified_backup(tmp_path: Path) -> None:
    fixture = next(item for item in HISTORICAL_FIXTURES if item.schema_version == 6)
    database_path = materialize_historical_database(fixture, tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        before_event = connection.execute(
            "SELECT subject_id, payload_json, payload_hash FROM events WHERE event_id = ?",
            (fixture.anchor_event_id,),
        ).fetchone()

    with (
        patch.object(
            Database,
            "_ensure_project_execution_columns",
            side_effect=RuntimeError("injected migration failure"),
        ),
        pytest.raises(RuntimeError, match="injected migration failure"),
    ):
        Database(database_path)

    backup_path = database_path.with_name(
        f"{database_path.name}.pre-migration-v{fixture.schema_version}.bak"
    )
    assert backup_path.is_file()
    with closing(sqlite3.connect(database_path)) as connection:
        marker = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        after_event = connection.execute(
            "SELECT subject_id, payload_json, payload_hash FROM events WHERE event_id = ?",
            (fixture.anchor_event_id,),
        ).fetchone()
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    with closing(sqlite3.connect(backup_path)) as backup:
        backup_marker = int(
            backup.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[
                0
            ]
        )
        backup_quick_check = str(backup.execute("PRAGMA quick_check").fetchone()[0])
    assert marker == backup_marker == fixture.schema_version
    assert tuple(after_event) == tuple(before_event)
    assert quick_check == backup_quick_check == "ok"


def test_schema_initialization_rolls_back_partial_sql_script(tmp_path: Path) -> None:
    database_path = tmp_path / "partial-initialize.sqlite3"
    original = Database._execute_sql_script

    def fail_after_first_statement(connection: sqlite3.Connection, script: str) -> None:
        if "CREATE TABLE IF NOT EXISTS schema_meta" in script:
            connection.execute("CREATE TABLE partial_schema(value TEXT NOT NULL)")
            raise RuntimeError("injected schema initialization failure")
        original(connection, script)

    with (
        patch.object(
            Database,
            "_execute_sql_script",
            staticmethod(fail_after_first_statement),
        ),
        pytest.raises(RuntimeError, match="injected schema initialization failure"),
    ):
        Database(database_path)

    # The failed transaction may leave an empty SQLite file, but it must not
    # leave a marker or any partially-created schema behind.
    assert database_path.exists()
    if database_path.stat().st_size:
        with sqlite3.connect(database_path) as connection:
            assert (
                connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                == []
            )

    repaired = Database(database_path)
    with repaired.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    assert "partial_schema" not in tables
    assert int(marker) == CURRENT_SCHEMA_VERSION
    assert quick_check == "ok"


def test_optional_feature_install_rolls_back_partial_sql_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "partial-optional.sqlite3"
    Database(database_path)
    original = Database._execute_sql_script

    def fail_optional_script(connection: sqlite3.Connection, script: str) -> None:
        if "CREATE TABLE IF NOT EXISTS secret_cleanup_queue" in script:
            connection.execute("CREATE TABLE partial_optional(value TEXT NOT NULL)")
            raise RuntimeError("injected optional feature failure")
        original(connection, script)

    with (
        patch.object(
            Database,
            "_execute_sql_script",
            staticmethod(fail_optional_script),
        ),
        pytest.raises(RuntimeError, match="injected optional feature failure"),
    ):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert "partial_optional" not in tables
    assert int(marker) == CURRENT_SCHEMA_VERSION

    repaired = Database(database_path)
    with repaired.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'secret_cleanup_queue'"
            ).fetchone()
            is not None
        )


def test_historical_fixture_range_covers_major_schema_eras() -> None:
    assert [fixture.schema_version for fixture in HISTORICAL_FIXTURES] == [6, 16, 21, 27, 31]
    assert len({fixture.source_commit for fixture in HISTORICAL_FIXTURES}) == len(
        HISTORICAL_FIXTURES
    )


def test_synthetic_history_profile_is_deterministic_and_integrity_valid(tmp_path: Path) -> None:
    profiles = load_synthetic_profiles(SYNTHETIC_PROFILES)
    profile_name = os.environ.get("NOYRA_M41_PROFILE", "smoke")
    if profile_name not in profiles:
        pytest.fail(f"unknown NOYRA_M41_PROFILE: {profile_name}")
    profile = profiles[profile_name]
    database_path = tmp_path / "synthetic.sqlite3"
    database = Database(database_path)

    history = populate_synthetic_history(database, profile)

    with database.connection() as connection:
        event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        training_count = int(
            connection.execute("SELECT COUNT(*) FROM training_records").fetchone()[0]
        )
        chain_count = int(
            connection.execute("SELECT COUNT(*) FROM event_chain_roots").fetchone()[0]
        )
        memory_count = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    chain = EventStore(database).verify_chain(history.subject_id)
    assert event_count == training_count == chain_count == profile.event_count
    assert memory_count == profile.memory_count
    assert chain["event_count"] == profile.event_count
    assert history.first_event_id == "evt_m41_000000000000"
    assert history.last_event_id == f"evt_m41_{profile.event_count - 1:012d}"
    assert history.logical_payload_bytes == profile.event_count * profile.payload_bytes
    assert history.memory_count == profile.memory_count
    assert database_path.is_relative_to(tmp_path)

    registry = IntegrityRegistry()
    for check_id in (
        "core.event_payloads",
        "core.event_chain",
        "core.event_causal_order",
        "mind.state",
    ):
        report = registry.run(
            database,
            history.subject_id,
            tmp_path,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=30,
            limits=IntegrityAuditLimits(),
            check_ids=(check_id,),
        )
        assert report.status == "ok", report.to_dict()
        assert report.p0 == ()
        assert report.p1 == ()


def test_synthetic_profiles_scale_without_changing_the_generator_contract() -> None:
    profiles = load_synthetic_profiles(SYNTHETIC_PROFILES)
    assert set(profiles) == {"smoke", "large", "soak"}
    assert profiles["smoke"].event_count < profiles["large"].event_count
    assert profiles["large"].event_count < profiles["soak"].event_count
    assert profiles["large"].payload_bytes >= profiles["smoke"].payload_bytes
    assert profiles["soak"].payload_bytes >= profiles["large"].payload_bytes


def test_fault_injector_and_state_probe_preserve_failure_trace() -> None:
    injector = FaultInjector((FaultRule("persist", occurrence=2, reason="database lost"),))
    probe = StateMachineProbe(
        "prepared",
        {
            "prepared": {"executing", "cancelled"},
            "executing": {"succeeded", "failed", "unknown"},
            "succeeded": set(),
            "failed": set(),
            "unknown": {"succeeded", "failed"},
            "cancelled": set(),
        },
    )
    probe.move("executing", evidence="worker acquired ownership")
    injector.checkpoint("persist")
    with pytest.raises(InjectedFault, match="persist@2: database lost"):
        injector.checkpoint("persist")
    probe.move("unknown", evidence="failure happened after ownership acquisition")

    assert injector.trace == ("persist:1", "persist:2")
    assert probe.state == "unknown"
    assert probe.transitions[0].previous == "prepared"
    with pytest.raises(AssertionError, match="forbidden state transition"):
        probe.move("cancelled", evidence="invalid terminal rewrite")


def test_deterministic_gate_coordinates_threads_without_sleep() -> None:
    gate = DeterministicGate()
    completed = threading.Event()

    def worker() -> None:
        gate.pause()
        completed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert gate.reached.wait(2)
    assert not completed.is_set()
    gate.release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert completed.is_set()


def test_sqlite_lock_helper_exposes_real_write_contention(tmp_path: Path) -> None:
    database_path = tmp_path / "locked.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        connection.commit()

    with (
        hold_sqlite_write_lock(database_path),
        closing(sqlite3.connect(database_path, timeout=0.01, isolation_level=None)) as rival,
        pytest.raises(sqlite3.OperationalError, match="locked"),
    ):
        rival.execute("BEGIN IMMEDIATE")


def _acceptance_issues() -> list[dict[str, Any]]:
    raw: Any = json.loads(ACCEPTANCE_MATRIX.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw["format_version"] == 1
    issues = raw["issues"]
    assert isinstance(issues, list)
    return issues


def test_acceptance_matrix_has_one_independent_contract_set_per_audit_issue() -> None:
    issues = _acceptance_issues()
    expected = {
        *(f"P1-{index:02d}" for index in range(1, 14)),
        *(f"P2-{index:02d}" for index in range(1, 19)),
        *(f"P3-{index:02d}" for index in range(1, 9)),
    }
    issue_ids = [str(issue["id"]) for issue in issues]
    assert set(issue_ids) == expected
    assert len(issue_ids) == len(set(issue_ids)) == 39

    contract_ids: list[str] = []
    for issue in issues:
        issue_id = str(issue["id"])
        assert issue["status"] in {"open", "implemented", "verified"}
        contracts = issue["contracts"]
        assert isinstance(contracts, list)
        assert len(contracts) >= 2
        for contract in contracts:
            contract_id = str(contract["id"])
            assert contract_id.startswith(f"{issue_id}.")
            assert str(contract["claim"]).strip()
            assert len(contract["evidence"]) >= 2
            contract_ids.append(contract_id)
    assert len(contract_ids) == len(set(contract_ids))


def test_acceptance_matrix_preserves_audit_origin_classification() -> None:
    issues = _acceptance_issues()
    counts: dict[str, int] = {}
    for issue in issues:
        origin = str(issue["origin"])
        counts[origin] = counts.get(origin, 0) + 1
    assert counts == {"native": 15, "residual": 13, "regression": 3, "maturity": 8}
    regressions = {str(issue["id"]) for issue in issues if issue["origin"] == "regression"}
    assert regressions == {"P1-03", "P1-06", "P2-12"}
