"""A synthetic trigger-only upgrade for recovery tests; never a shipped migration."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from unittest.mock import patch

from noyra.core import Database
from noyra.core import database as database_module
from noyra.core import workflow_source_recovery as recovery
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS

TARGET = 117
SQL = """
CREATE TRIGGER synthetic_workflow_recovery_probe
BEFORE UPDATE OF workflow_id ON workflow_export_publications
WHEN NEW.workflow_id IS NOT OLD.workflow_id
BEGIN SELECT RAISE(ABORT,'synthetic recovery fixture'); END;
"""


def measured_pair(database: Database) -> recovery.RecoverySchemaPair:
    with database.read_snapshot() as snapshot:
        version = int(
            snapshot.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
                0
            ]
        )
        assert version == 116
        original = Database.schema_ddl_fingerprint(snapshot)
        assert original == _SCHEMA_DDL_FINGERPRINTS[116]
        structure = Database.schema_structure_fingerprint(snapshot)
        # The sealed snapshot stays query-only; measure DDL on a separate copy.
        with closing(sqlite3.connect(":memory:")) as candidate:
            candidate.row_factory = sqlite3.Row
            snapshot.backup(candidate)
            Database._execute_sql_script(candidate, SQL)
            target = Database.schema_ddl_fingerprint(candidate)
            assert target != original
            assert Database.schema_structure_fingerprint(candidate) == structure
    return recovery.RecoverySchemaPair(116, original, TARGET, target, structure)


@contextmanager
def synthetic_runtime(
    pair: recovery.RecoverySchemaPair, *, approved: bool = True
) -> Iterator[None]:
    # Only these test-scoped contracts make the synthetic target recognizable.
    # Production has no new migration, version or approved recovery pair in M2.2.
    with (
        patch.object(database_module, "CURRENT_SCHEMA_VERSION", TARGET),
        patch.dict(database_module.MIGRATIONS, {TARGET: SQL}),
        patch.dict(_SCHEMA_DDL_FINGERPRINTS, {TARGET: pair.target_ddl}),
        patch.dict(_SCHEMA_STRUCTURE_FINGERPRINTS, {TARGET: pair.structure}),
        patch.object(recovery, "_REVIEWED_SCHEMA_PAIRS", (pair,) if approved else ()),
    ):
        yield
