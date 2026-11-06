from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from noyra.core.types import content_hash


def test_114_upgrade_and_rollback_preserve_old_workflow_rows(tmp_path: Path) -> None:
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 114):
        database = Database(tmp_path / "old.sqlite3", strict_schema_contract=True)
    IdentityStore(database).ensure("one", content_hash("one"))
    with database.transaction() as c:
        c.execute(
            "INSERT INTO workflow_checkpoints VALUES('old','one','fixture',1,'running',"
            "'{}',?,'old','2026-11-06T00:00:00+00:00')",
            (content_hash({}),),
        )
    with database.read_transaction() as c:
        before = [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")]
        assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[114]
    execute = Database._execute_sql_script

    def failed(c: Any, sql: str) -> None:
        execute(c, sql)
        if sql == MIGRATIONS[115]:
            raise OSError("synthetic migration interruption")

    with patch.object(Database, "_execute_sql_script", side_effect=failed), pytest.raises(OSError):
        Database(database.path)
    with database.read_transaction() as c:
        assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[114]
        assert [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")] == before
    for _ in range(2):
        current = Database(database.path, strict_schema_contract=True)
        with current.read_transaction() as c:
            assert (
                Database.schema_ddl_fingerprint(c)
                == _SCHEMA_DDL_FINGERPRINTS[CURRENT_SCHEMA_VERSION]
            )
            assert [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")] == before
            assert c.execute("SELECT COUNT(*) FROM workflow_export_publications").fetchone()[0] == 0
