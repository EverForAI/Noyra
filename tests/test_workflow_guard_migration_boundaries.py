from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core import Database
from noyra.core import database as database_module
from noyra.core.errors import SchemaContractError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from noyra.core.schema.workflow_publication_guards import WORKFLOW_PUBLICATION_GUARDS_SCHEMA
from test_m2_publication_migration import archive_key as archive_key
from test_m2_publication_migration import clone, prepare, snapshot
from test_m2_publication_migration import schemas as schemas

TRIGGER = "preserve_workflow_export_transition"
DROP = f"DROP TRIGGER {TRIGGER};"
REPLACEMENT = WORKFLOW_PUBLICATION_GUARDS_SCHEMA.split(
    "DROP TRIGGER IF EXISTS validate_workflow_export_initial_state;", 1
)[0]


@pytest.mark.parametrize("kind", ["export", "increment"])
@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown", "accepted"])
def test_complete_guard_replay_is_safe_and_normal_reopen_does_not_rerun_migration(
    schemas: dict[int, Path], tmp_path: Path, kind: str, state: str
) -> None:
    old = clone(schemas[116], tmp_path / "legacy.sqlite3")
    publication = prepare(old, kind, state)
    before = snapshot(old)
    execute = Database._execute_sql_script
    stages: list[int] = []

    def count_stage(connection: sqlite3.Connection, script: str) -> None:
        if script == WORKFLOW_PUBLICATION_GUARDS_SCHEMA:
            stages.append(117)
        execute(connection, script)

    with patch.object(Database, "_execute_sql_script", staticmethod(count_stage)):
        for _ in range(3):
            current = Database(old.path, strict_schema_contract=True)
            after = snapshot(current)
            assert after["version"] == str(database_module.CURRENT_SCHEMA_VERSION)
            assert after["ddl"] == _SCHEMA_DDL_FINGERPRINTS[database_module.CURRENT_SCHEMA_VERSION]
            for table, rows in before["rows"].items():
                if table not in {"schema_meta", "persistent_features"}:
                    assert after["rows"][table] == rows, table
    assert stages == [117]
    frozen = snapshot(current)
    # This is a controlled SQL replay proof, not an operator repair procedure.
    for _ in range(3):
        with current.transaction() as connection:
            execute(connection, WORKFLOW_PUBLICATION_GUARDS_SCHEMA)
        current.assert_schema_contract()
        assert snapshot(current) == frozen
    with current.read_transaction() as connection:
        assert (
            publication.catalog.get(connection, publication.record["publication_id"])
            == publication.record
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


@pytest.mark.parametrize("prefix", [DROP, REPLACEMENT], ids=["after-drop", "after-recreate"])
@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown", "accepted"])
def test_partial_guard_replacement_rolls_back_then_real_upgrade_succeeds(
    schemas: dict[int, Path], tmp_path: Path, prefix: str, state: str
) -> None:
    old = clone(schemas[116], tmp_path / "interrupted.sqlite3")
    prepare(old, "increment", state)
    before = snapshot(old)
    execute = Database._execute_sql_script

    def interrupt(connection: sqlite3.Connection, script: str) -> None:
        if script == WORKFLOW_PUBLICATION_GUARDS_SCHEMA:
            execute(connection, prefix)
            assert connection.in_transaction
            raise OSError("synthetic interruption in guard replacement")
        execute(connection, script)

    with (
        patch.object(Database, "_execute_sql_script", staticmethod(interrupt)),
        pytest.raises(OSError, match="synthetic interruption"),
    ):
        Database(old.path, strict_schema_contract=True)
    assert snapshot(old) == before
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 116):
        Database(old.path, strict_schema_contract=True)
    current = Database(old.path, strict_schema_contract=True)
    current.assert_schema_contract()
    assert snapshot(current)["version"] == str(database_module.CURRENT_SCHEMA_VERSION)


@pytest.mark.parametrize("strict", [False, True])
def test_missing_required_old_trigger_is_rejected_without_repairing_the_damaged_copy(
    schemas: dict[int, Path], tmp_path: Path, strict: bool
) -> None:
    old = clone(schemas[116], tmp_path / "damaged.sqlite3")
    prepare(old, "increment", "accepted")
    with old.transaction() as connection:
        connection.execute(DROP)
    damaged = snapshot(old)
    with pytest.raises(SchemaContractError, match="DDL mismatch"):
        Database(old.path, initialize=False, strict_schema_contract=True)
    with pytest.raises(sqlite3.OperationalError, match=f"no such trigger: {TRIGGER}"):
        Database(old.path, strict_schema_contract=strict)
    assert snapshot(old) == damaged
    with old.read_transaction() as connection:
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (TRIGGER,)
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.skipif(os.name != "posix", reason="Native SQLite process-crash rollback")
@pytest.mark.parametrize("prefix", [DROP, REPLACEMENT], ids=["after-drop", "after-recreate"])
def test_process_exit_inside_guard_replacement_restores_original_schema_and_all_rows(
    schemas: dict[int, Path], tmp_path: Path, prefix: str
) -> None:
    old = clone(schemas[116], tmp_path / "crash.sqlite3")
    prepare(old, "increment", "published_durability_unknown")
    before = snapshot(old)
    code = """
import os, sys
from unittest.mock import patch
from noyra.core import Database
from noyra.core.schema.workflow_publication_guards import WORKFLOW_PUBLICATION_GUARDS_SCHEMA
execute = Database._execute_sql_script
def crash(c, script):
    if script == WORKFLOW_PUBLICATION_GUARDS_SCHEMA:
        execute(c, sys.argv[2])
        assert c.in_transaction
        os._exit(87)
    execute(c, script)
with patch.object(Database, '_execute_sql_script', staticmethod(crash)):
    Database(sys.argv[1], strict_schema_contract=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(old.path), prefix],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 87, result.stderr
    assert snapshot(old) == before
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 116):
        Database(old.path, strict_schema_contract=True)
    Database(old.path, strict_schema_contract=True).assert_schema_contract()
