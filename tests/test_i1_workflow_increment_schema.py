from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS
from noyra.core.errors import IntegrityError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from noyra.core.types import canonical_json, content_hash
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_i1_workflow_increment_catalog import (
    MANIFEST,
    SUBJECT,
    accepted,
    advance,
    prepare,
    source,
)
from test_i1_workflow_increment_catalog import database as database
from test_i1_workflow_increment_catalog import metadata as metadata
from test_i1_workflow_increment_catalog import template as template


def test_old_115_migration_rolls_back_then_preserves_old_base_and_refuses_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from noyra.core.archive import ArchiveKeyring
    from test_d2b_workflow_bundle import seed

    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 115):
        old = Database(tmp_path / "old.sqlite3", strict_schema_contract=True)
    IdentityStore(old).ensure(SUBJECT, content_hash("export-genesis"))
    seed(old, 3)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(old, SUBJECT)
    original = source(old)
    catalog = WorkflowExportCatalog(old, SUBJECT)
    with old.transaction() as c:
        base = catalog.prepare(c, original, content_hash("old-base-key"), ring)
        base = catalog.finish(c, base, MANIFEST, durable=True)
    tables = ("workflow_checkpoints", "workflow_export_publications", "events", "event_chain_roots")
    with old.read_transaction() as c:
        before = {table: [dict(r) for r in c.execute("SELECT * FROM " + table)] for table in tables}
    execute = Database._execute_sql_script

    def interrupted(c: Any, sql: str) -> None:
        execute(c, sql)
        if sql == MIGRATIONS[116]:
            raise OSError("synthetic metadata migration interruption")

    with (
        patch.object(Database, "_execute_sql_script", side_effect=interrupted),
        pytest.raises(OSError),
    ):
        Database(old.path)
    with old.read_transaction() as c:
        assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[115]
        assert (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='workflow_increment_publications'"
            ).fetchone()
            is None
        )
        assert before == {
            table: [dict(r) for r in c.execute("SELECT * FROM " + table)] for table in tables
        }
    for _ in range(2):
        current = Database(old.path, strict_schema_contract=True)
        with current.read_transaction() as c:
            assert (
                Database.schema_ddl_fingerprint(c)
                == _SCHEMA_DDL_FINGERPRINTS[CURRENT_SCHEMA_VERSION]
            )
            assert before == {
                table: [dict(r) for r in c.execute("SELECT * FROM " + table)] for table in tables
            }
            assert WorkflowExportCatalog(current, SUBJECT).get(c, base["publication_id"]) == base
            assert c.execute("PRAGMA foreign_key_check").fetchone() is None
    fresh = advance(current)
    with current.transaction() as c, pytest.raises(IntegrityError, match="scope or schema"):
        WorkflowIncrementCatalog(current, SUBJECT).prepare(
            c, fresh, base["publication_id"], content_hash("cross-schema"), ring
        )
    with (
        patch.object(database_module, "CURRENT_SCHEMA_VERSION", 115),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(current.path)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE workflow_increment_publications SET source_hash=source_hash",
        "UPDATE workflow_increment_publications SET parent_base_id=parent_base_id",
        "UPDATE workflow_increment_publications SET first_version=first_version",
        "DELETE FROM workflow_increment_publications",
        "UPDATE workflow_increment_requests SET expected_version=expected_version",
        "DELETE FROM workflow_increment_requests",
    ],
)
def test_dependency_source_and_request_records_are_sql_immutable(
    metadata: tuple[Any, ...],
    sql: str,
) -> None:
    db, _catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(sql)
    assert prepare(metadata) == record


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "accepted"),
        ("parent_base_id", "wexport_" + "0" * 64),
        ("first_version", 5),
        ("chain_depth", 2),
        ("last_version", 3),
        ("parent_increment_id", "wdelta_" + "0" * 64),
        ("subject_id", "foreign-subject"),
    ],
)
def test_raw_sql_cannot_insert_a_terminal_or_invalid_parent_dependency(
    metadata: tuple[Any, ...],
    field: str,
    value: Any,
) -> None:
    db, _catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.read_transaction() as c:
        row = dict(c.execute("SELECT * FROM workflow_increment_publications").fetchone())
    row["publication_id"] = "wdelta_" + "0" * 64
    row[field] = value
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO workflow_increment_publications("
            + ",".join(row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    assert record["status"] == "prepared"


def test_published_manifest_and_accepted_rows_are_sql_frozen(metadata: tuple[Any, ...]) -> None:
    db, catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.transaction() as c:
        published = catalog.finish(c, record, MANIFEST, durable=False)
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "UPDATE workflow_increment_publications SET status='accepted',manifest_json=?,"
            "receipt_event_id=?",
            (canonical_json({**MANIFEST, "stored_hash": "c" * 64}), "not-a-real-receipt"),
        )
    with db.transaction() as c:
        catalog.finish(c, published, MANIFEST, durable=True)
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE workflow_increment_publications SET updated_at=updated_at")


def test_request_foreign_key_and_intent_guard_protect_parent_and_subject(
    metadata: tuple[Any, ...],
) -> None:
    db, _catalog, _ring, _base = metadata
    advance(db)
    accepted(metadata)
    with db.read_transaction() as c:
        request = dict(c.execute("SELECT * FROM workflow_increment_requests").fetchone())
    request["request_hash"] = "0" * 64
    request["parent_id"] = "wrong-parent"
    with db.transaction() as c, pytest.raises(sqlite3.IntegrityError, match="intent differs"):
        c.execute(
            "INSERT INTO workflow_increment_requests("
            + ",".join(request)
            + ") VALUES("
            + ",".join("?" for _ in request)
            + ")",
            tuple(request.values()),
        )
    # The historical migration's bytes remain identical to the frozen contract.
    contract = json.loads(
        (Path(__file__).parent / "contracts/database-definition-contract.json").read_text()
    )
    import hashlib

    assert (
        hashlib.sha256(MIGRATIONS[115].encode()).hexdigest() == contract["migration_sha256"]["115"]
    )
