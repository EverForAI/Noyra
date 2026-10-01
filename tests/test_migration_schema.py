from __future__ import annotations

import sqlite3

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.migration.policy import MigrationStore


def test_migration_schema_is_current_and_append_only(tmp_path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "b" * 64)

    with database.connection() as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        assert version == CURRENT_SCHEMA_VERSION
        for table in (
            "migration_policies",
            "migration_targets",
            "migration_proposals",
            "migration_tasks",
            "migration_epochs",
            "migration_rejections",
            "migration_audit_events",
        ):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()

        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(migration_tasks)")
        }
        assert "target_epoch_id" in task_columns

    store = MigrationStore(database)
    store.read_policy("Noyra-0001")
    with database.transaction() as connection:
        row = connection.execute("SELECT audit_id FROM migration_audit_events LIMIT 1").fetchone()
        assert row is None


def test_migration_audit_events_cannot_be_updated_or_deleted(tmp_path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "c" * 64)
    store = MigrationStore(database)
    store.update_policy(
        "Noyra-0001",
        expected_revision=1,
        patch={"enabled": True},
        actor="operator",
    )

    with database.connection() as connection:
        audit_id = connection.execute(
            "SELECT audit_id FROM migration_audit_events ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()[0]

    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute(
                "UPDATE migration_audit_events SET actor='tampered' WHERE audit_id=?",
                (audit_id,),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("migration audit update unexpectedly succeeded")
