from __future__ import annotations

from pathlib import Path

import pytest

from noyra.core.database import CURRENT_SCHEMA_VERSION, Database, _canonical_schema_sql


def test_search_routing_schema_is_installed_and_registered_by_database(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.connection() as connection:
        schema_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        objects = {
            (str(row["type"]), str(row["name"]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name IN (?, ?)",
                ("search_provider_routing", "idx_search_provider_routing_order"),
            )
        }
        feature = connection.execute(
            "SELECT feature_version, ddl_fingerprint FROM persistent_features "
            "WHERE feature_id='search_provider_routing'"
        ).fetchone()

    assert schema_version == CURRENT_SCHEMA_VERSION
    assert objects == {
        ("table", "search_provider_routing"),
        ("index", "idx_search_provider_routing_order"),
    }
    assert feature is not None
    assert int(feature["feature_version"]) == 1
    assert len(str(feature["ddl_fingerprint"])) == 64


def test_missing_additive_index_fails_closed_on_database_reopen(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.transaction() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_model_calls_subject_status")

    with pytest.raises(RuntimeError, match="schema contract"):
        Database(database.path)


def test_schema_contract_rejects_modified_persistent_feature_ddl(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.transaction() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_search_provider_routing_order")
        connection.execute("DROP TABLE IF EXISTS search_provider_routing")
        connection.execute(
            "CREATE TABLE search_provider_routing ("
            "config_id TEXT PRIMARY KEY, "
            "priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 1000), "
            "weight INTEGER NOT NULL CHECK(weight BETWEEN 1 AND 1000), "
            "updated_at TEXT NOT NULL, state_hash TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX idx_search_provider_routing_order "
            "ON search_provider_routing(config_id, priority)"
        )

    with pytest.raises(RuntimeError, match=r"registry fingerprint|schema contract"):
        Database(database.path)


def test_schema_contract_marker_rejects_same_version_fingerprint_change(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_ddl_contract'",
            (f"{CURRENT_SCHEMA_VERSION}:{'0' * 64}",),
        )

    with pytest.raises(RuntimeError, match="schema contract marker"):
        Database(database.path)


def test_schema_76_marker_migrates_to_the_reviewed_schema_77_contract(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.transaction() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_search_provider_routing_order")
        connection.execute("DROP TABLE IF EXISTS search_provider_routing")
        connection.execute("UPDATE schema_meta SET value='76' WHERE key='schema_version'")
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_ddl_contract'",
            (f"76:{'a' * 64}",),
        )
        connection.execute(
            "DELETE FROM persistent_features WHERE feature_id='search_provider_routing'"
        )

    upgraded = Database(database.path)

    with upgraded.connection() as connection:
        schema_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        contract = str(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_ddl_contract'"
            ).fetchone()[0]
        )

    assert schema_version == 77
    assert contract.startswith("77:")


def test_missing_persistent_feature_marker_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")

    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM persistent_features WHERE feature_id='search_provider_routing'"
        )

    with (
        database.connection() as connection,
        pytest.raises(RuntimeError, match="persistent schema feature search_provider_routing"),
    ):
        database.require_persistent_feature(connection, "search_provider_routing")


def test_schema_ddl_canonicalizer_ignores_formatting_but_preserves_literals() -> None:
    assert _canonical_schema_sql(
        "CREATE TABLE x (value TEXT DEFAULT 'some value') -- comment\n"
    ) == _canonical_schema_sql(" CREATE TABLE x(value TEXT DEFAULT 'some value') ")
    assert _canonical_schema_sql("CREATE TABLE x(value TEXT DEFAULT 'first')") != (
        _canonical_schema_sql("CREATE TABLE x(value TEXT DEFAULT 'second')")
    )
