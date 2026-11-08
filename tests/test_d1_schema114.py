from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, EventStore, IdentityStore
from noyra.core import database as database_module
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS
from noyra.core.errors import SchemaContractError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from noyra.core.types import content_hash
from noyra.mind import MemoryStore

INDEXES = {
    "idx_memories_subject_rank",
    "idx_memories_subject_type_rank",
    "idx_workflow_checkpoints_subject_history",
}


@pytest.fixture
def old_database(tmp_path: Path) -> Iterator[Database]:
    # Actually execute only the historical migrations, not an artificially
    # relabelled current schema. Its frozen fingerprint proves the fixture.
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 113):
        database = Database(tmp_path / "schema113.sqlite3", strict_schema_contract=True)
    IdentityStore(database).ensure("one", content_hash("schema114"))
    event = EventStore(database).append("one", "experience", "fixture", {})
    MemoryStore(database).create(
        "one",
        "semantic",
        "Historical source memory",
        salience=0.7,
        confidence=0.8,
        source_event_ids=(event.event_id,),
    )
    with database.read_transaction() as c:
        assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[113]
        assert (
            int(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == 113
        )
    yield database


def snapshot(database: Database) -> dict[str, list[tuple[Any, ...]]]:
    with database.read_transaction() as c:
        return {
            table: [tuple(r) for r in c.execute("SELECT * FROM " + table)]
            for table in ("subject_identity", "memories", "memory_revisions", "events")
        }


def test_historical113_upgrades_idempotently_with_unchanged_source_rows(
    old_database: Database,
) -> None:
    before = snapshot(old_database)
    upgraded = Database(old_database.path, strict_schema_contract=True)
    assert snapshot(upgraded) == before
    with upgraded.read_transaction() as c:
        assert (
            int(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == CURRENT_SCHEMA_VERSION
        )
        found = {row[0] for row in c.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
        assert INDEXES.issubset(found)
        ddl = Database.schema_ddl_fingerprint(c)
        assert ddl == _SCHEMA_DDL_FINGERPRINTS[CURRENT_SCHEMA_VERSION]
    reopened = Database(old_database.path, strict_schema_contract=True)
    with reopened.read_transaction() as c:
        assert Database.schema_ddl_fingerprint(c) == ddl
    assert snapshot(reopened) == before


def test_interrupted_index_migration_restores_113_then_can_retry(old_database: Database) -> None:
    before = snapshot(old_database)
    execute = Database._execute_sql_script

    def interrupted(c: Any, script: str) -> None:
        if script == MIGRATIONS[114]:
            # Fail after a real DDL write, inside the migration transaction.
            c.execute(
                "CREATE INDEX idx_memories_subject_rank ON memories("
                "subject_id,status,salience DESC,confidence DESC,updated_at DESC)"
            )
            raise OSError("Synthetic second-index interruption")
        execute(c, script)

    with (
        patch.object(Database, "_execute_sql_script", side_effect=interrupted),
        pytest.raises(OSError, match="second-index interruption"),
    ):
        Database(old_database.path)
    assert snapshot(old_database) == before
    with old_database.read_transaction() as c:
        assert (
            int(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == 113
        )
        assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[113]
    assert snapshot(Database(old_database.path, strict_schema_contract=True)) == before


def test_structural_index_drift_is_rejected_and_newer_schema_is_not_downgraded(
    old_database: Database,
) -> None:
    upgraded = Database(old_database.path)
    before = snapshot(upgraded)
    with (
        patch.object(database_module, "CURRENT_SCHEMA_VERSION", 113),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(upgraded.path)
    assert snapshot(upgraded) == before
    with upgraded.transaction() as c:
        c.execute("DROP INDEX idx_memories_subject_rank")
        c.execute("CREATE INDEX idx_memories_subject_rank ON memories(subject_id,content)")
    with pytest.raises(SchemaContractError):
        Database(upgraded.path, strict_schema_contract=True)


def test_historical_migration_hashes_stay_frozen() -> None:
    import json

    baseline = json.loads(
        (Path(__file__).parent / "contracts/database-definition-contract.json").read_text()
    )
    for version, expected in baseline["migration_sha256"].items():
        assert hashlib.sha256(MIGRATIONS[int(version)].encode()).hexdigest() == expected
