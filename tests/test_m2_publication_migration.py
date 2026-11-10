from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from noyra.core.types import canonical_json, content_hash
from noyra.core.workflow_bundle import inspect_source
from noyra.core.workflow_export_catalog import FIELDS as BASE_FIELDS
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_increment_catalog import FIELDS as INCREMENT_FIELDS
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_manifest import validate_manifest_descriptor
from noyra.core.workflow_publication_upgrade import validate_publication_upgrade
from test_d2b_workflow_bundle import nothing, seed
from test_i1_workflow_increment_catalog import MANIFEST, SUBJECT, advance


@pytest.fixture(scope="module")
def schemas(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    paths = {}
    for version in (115, 116, 117):
        with patch.object(database_module, "CURRENT_SCHEMA_VERSION", version):
            db = Database(
                tmp_path_factory.mktemp("publication-schema") / "template.sqlite3",
                strict_schema_contract=True,
            )
        IdentityStore(db).ensure(SUBJECT, content_hash("export-genesis"))
        seed(db, 3)
        paths[version] = db.path
    return paths


@pytest.fixture(autouse=True)
def archive_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )


def clone(source: Path, destination: Path) -> Database:
    with closing(sqlite3.connect(source)) as a, closing(sqlite3.connect(destination)) as b:
        a.backup(b)
    return Database(destination, initialize=False, strict_schema_contract=True)


@dataclass
class Publication:
    database: Database
    catalog: WorkflowExportCatalog | WorkflowIncrementCatalog
    record: dict[str, Any]
    kind: str

    @property
    def table(self) -> str:
        return "workflow_" + self.kind + "_publications"

    @property
    def fields(self) -> tuple[str, ...]:
        return BASE_FIELDS if self.kind == "export" else INCREMENT_FIELDS

    def finish(self, *, durable: bool) -> dict[str, Any]:
        with self.database.transaction() as c:
            result = self.catalog.finish(c, self.record, MANIFEST, durable=durable)
        self.record = result
        return result


def prepare(db: Database, kind: str = "export", state: str = "prepared") -> Publication:
    ring = ArchiveKeyring.from_env()
    ring.record_revision(db, SUBJECT)
    with db.read_snapshot() as c:
        source = inspect_source(c, db, SUBJECT, "workflow", nothing)
    base = WorkflowExportCatalog(db, SUBJECT)
    with db.transaction() as c:
        record = base.prepare(c, source, content_hash("migration-base-request"), ring)
    value = Publication(db, base, record, "export")
    if kind == "increment":
        parent = value.finish(durable=True)
        suffix = advance(db)
        increment = WorkflowIncrementCatalog(db, SUBJECT)
        with db.transaction() as c:
            record = increment.prepare(
                c, suffix, parent["publication_id"], content_hash("migration-child-request"), ring
            )
        value = Publication(db, increment, record, "increment")
    if state != "prepared":
        value.finish(durable=state == "accepted")
    return value


def snapshot(db: Database) -> dict[str, Any]:
    with db.read_transaction() as c:
        tables = [
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            "ddl": Database.schema_ddl_fingerprint(c),
            "version": c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
            "rows": {
                table: sorted(
                    [dict(r) for r in c.execute('SELECT * FROM "' + table + '"')],
                    key=lambda row: canonical_json(
                        {k: v.hex() if isinstance(v, bytes) else v for k, v in row.items()}
                    ),
                )
                for table in tables
            },
        }


@contextmanager
def corrupt_copy(c: Any, table: str) -> Iterator[None]:
    # Corruption fixtures restore all original DDL; never weaken a production path.
    triggers = list(
        c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
    )
    for trigger in triggers:
        c.execute('DROP TRIGGER "' + trigger["name"] + '"')
    try:
        yield
    finally:
        for trigger in triggers:
            c.execute(trigger["sql"])


@pytest.fixture(params=("export", "increment"))
def publication(
    schemas: dict[int, Path], tmp_path: Path, request: pytest.FixtureRequest
) -> Publication:
    return prepare(clone(schemas[117], tmp_path / "current.sqlite3"), request.param)


@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
def test_every_column_update_and_delete_are_protected_in_all_states(
    publication: Publication, state: str
) -> None:
    if state != "prepared":
        publication.finish(durable=state == "accepted")
    before = snapshot(publication.database)
    for column in (*publication.fields, "state_hash"):
        for expression, parameters in ((f'"{column}"', ()), ("?", ("changed-value",))):
            with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
                c.execute(
                    "UPDATE "
                    + publication.table
                    + ' SET "'
                    + column
                    + '"='
                    + expression
                    + " WHERE publication_id=?",
                    (*parameters, publication.record["publication_id"]),
                )
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute("DELETE FROM " + publication.table)
    assert snapshot(publication.database) == before


@pytest.mark.parametrize("before_state", ("prepared", "published_durability_unknown", "accepted"))
@pytest.mark.parametrize("after_state", ("prepared", "published_durability_unknown", "accepted"))
def test_complete_state_transition_matrix(
    publication: Publication, before_state: str, after_state: str
) -> None:
    if before_state != "prepared":
        publication.finish(durable=before_state == "accepted")
    allowed = (before_state, after_state) in {
        ("prepared", "published_durability_unknown"),
        ("prepared", "accepted"),
        ("published_durability_unknown", "accepted"),
    }
    if allowed:
        assert publication.finish(durable=after_state == "accepted")["status"] == after_state
    else:
        before = snapshot(publication.database)
        with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
            c.execute("UPDATE " + publication.table + " SET status=?", (after_state,))
        assert snapshot(publication.database) == before


INVALID = (
    {"stored_bytes": -1},
    {"stored_bytes": 36},
    {"stored_bytes": 1048614},
    {"stored_bytes": True},
    {"stored_bytes": 137.0},
    {"stored_bytes": "137"},
    {"stored_hash": "a" * 63},
    {"stored_hash": "A" * 64},
    {"stored_hash": 1},
    {"stored_hash": "a" * 63 + "\0"},
    {"stored_hash": "a" * 64 + "\0suffix"},
    {"stored_hash": "a" * 63 + "é"},
    {"plaintext_hash": None},
    {"plaintext_hash": "g" * 64},
    {"name": "../manifest.enc"},
    {"extra": 1},
)


@pytest.mark.parametrize("patches", INVALID)
def test_sql_and_application_descriptor_constraints_reject_the_same_bad_values(
    publication: Publication, patches: dict[str, Any]
) -> None:
    bad = {**MANIFEST, **patches}
    with pytest.raises(IntegrityError):
        validate_manifest_descriptor(bad, maximum=1024 * 1024)
    raw = {key: publication.record[key] for key in publication.fields}
    raw.update(status="published_durability_unknown", manifest_json=canonical_json(bad))
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "UPDATE " + publication.table + " SET status=?,manifest_json=?,state_hash=?",
            (raw["status"], raw["manifest_json"], content_hash(raw)),
        )


@pytest.mark.parametrize(
    "encoded",
    (
        "[]",
        "{}",
        "null",
        '{"name":"manifest.enc"}',
        '{"name":"manifest.enc","stored_bytes":137,"stored_hash":"a","stored_hash":"b"}',
    ),
)
def test_sql_rejects_wrong_shape_missing_and_duplicate_keys(
    publication: Publication, encoded: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "UPDATE " + publication.table + " SET status='published_durability_unknown',"
            "manifest_json=?",
            (encoded,),
        )


@pytest.mark.parametrize("size", (37, 1048613))
def test_legal_descriptor_bounds_are_accepted(publication: Publication, size: int) -> None:
    with publication.database.transaction() as c:
        assert (
            publication.catalog.finish(
                c, publication.record, {**MANIFEST, "stored_bytes": size}, durable=False
            )["status"]
            == "published_durability_unknown"
        )


def test_terminal_rows_cannot_be_inserted_and_unknown_manifest_cannot_be_changed(
    publication: Publication,
) -> None:
    raw = {k: publication.record[k] for k in publication.fields}
    raw.update(
        publication_id=("wexport_" if publication.kind == "export" else "wdelta_") + "0" * 64,
        source_hash="0" * 64,
        status="published_durability_unknown",
        manifest_json=canonical_json(MANIFEST),
    )
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "INSERT INTO "
            + publication.table
            + "("
            + ",".join(raw)
            + ",state_hash) VALUES("
            + ",".join("?" for _ in range(len(raw) + 1))
            + ")",
            (*raw.values(), content_hash(raw)),
        )
    publication.finish(durable=False)
    before = snapshot(publication.database)
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "UPDATE " + publication.table + " SET manifest_json=?,state_hash=?",
            (canonical_json({**MANIFEST, "stored_hash": "c" * 64}), "0" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "UPDATE " + publication.table + " SET status='accepted',manifest_json=?,"
            "receipt_event_id=?",
            (canonical_json({**MANIFEST, "stored_hash": "c" * 64}), "wrong"),
        )
    assert snapshot(publication.database) == before


def test_unbound_acceptance_receipt_cannot_authorize_raw_promotion(
    publication: Publication,
) -> None:
    publication.finish(durable=False)
    with publication.database.read_transaction() as c:
        other = c.execute("SELECT event_id FROM events LIMIT 1").fetchone()[0]
    for receipt in (None, other, "missing-receipt"):
        with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
            c.execute(
                "UPDATE " + publication.table + " SET status='accepted',receipt_event_id=?",
                (receipt,),
            )
    assert publication.finish(durable=True)["status"] == "accepted"


@pytest.mark.parametrize("version,kind", ((115, "export"), (116, "export"), (116, "increment")))
@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
def test_legacy_upgrade_preserves_exact_rows_and_reopens_without_duplicate_receipts(
    schemas: dict[int, Path], tmp_path: Path, version: int, kind: str, state: str
) -> None:
    old = clone(schemas[version], tmp_path / "legacy.sqlite3")
    publication = prepare(old, kind, state)
    before = snapshot(old)
    for _ in range(2):
        current = Database(old.path, strict_schema_contract=True)
        after = snapshot(current)
        assert after["version"] == "117" and after["ddl"] == _SCHEMA_DDL_FINGERPRINTS[117]
        for table, rows in before["rows"].items():
            if table not in {"schema_meta", "persistent_features"}:
                assert after["rows"][table] == rows, table
        with current.read_transaction() as c:
            assert (
                publication.catalog.get(c, publication.record["publication_id"])
                == publication.record
            )
            assert c.execute("PRAGMA foreign_key_check").fetchone() is None
    with (
        patch.object(database_module, "CURRENT_SCHEMA_VERSION", version),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(old.path)


@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
@pytest.mark.parametrize("point", ("after_ddl", "after_version", "after_optional"))
def test_migration_failure_restores_old_contract_and_all_rows(
    schemas: dict[int, Path], tmp_path: Path, state: str, point: str
) -> None:
    old = clone(schemas[116], tmp_path / "rollback.sqlite3")
    prepare(old, "increment", state)
    before = snapshot(old)
    execute = Database._execute_sql_script
    set_version = Database._set_schema_version
    optional = Database._ensure_optional_features

    def fail_sql(c: Any, sql: str) -> None:
        execute(c, sql)
        if point == "after_ddl" and sql == database_module.MIGRATIONS[117]:
            raise OSError("synthetic DDL interruption")

    def fail_version(self: Database, c: Any, version: int) -> None:
        set_version(self, c, version)
        if point == "after_version" and version == 117:
            raise OSError("synthetic marker interruption")

    def fail_optional(self: Database) -> None:
        optional(self)
        if point == "after_optional":
            raise OSError("synthetic post-migration interruption")

    with (
        patch.object(Database, "_execute_sql_script", staticmethod(fail_sql)),
        patch.object(Database, "_set_schema_version", fail_version),
        patch.object(Database, "_ensure_optional_features", fail_optional),
        pytest.raises(OSError),
    ):
        Database(old.path, strict_schema_contract=True)
    assert snapshot(old) == before
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 116):
        Database(old.path, strict_schema_contract=True)
    assert Database(old.path, strict_schema_contract=True)


@pytest.mark.parametrize(
    "kind,damage",
    [
        (kind, damage)
        for kind in ("export", "increment")
        for damage in (
            "descriptor",
            "state_hash",
            "source",
            "prepared_receipt",
            "accepted_receipt",
            "parent_reference",
            "request_hash",
        )
        if kind == "increment" or damage not in {"parent_reference", "request_hash"}
    ],
)
def test_corrupt_old_copy_is_rejected_without_repairing_or_resigning_evidence(
    schemas: dict[int, Path], tmp_path: Path, kind: str, damage: str
) -> None:
    old = clone(schemas[116], tmp_path / "corrupt.sqlite3")
    publication = prepare(old, kind, "accepted")
    with old.transaction() as c:
        if damage in {"prepared_receipt", "accepted_receipt"}:
            event = (
                "evt_wexport_prepared_" + publication.record["source_hash"]
                if kind == "export"
                else "evt_wincrement_prepared_" + publication.record["publication_id"]
            )
            if damage == "accepted_receipt":
                event = publication.record["receipt_event_id"]
            with corrupt_copy(c, "events"):
                c.execute("UPDATE events SET payload_hash=? WHERE event_id=?", ("0" * 64, event))
        elif damage == "request_hash":
            with corrupt_copy(c, "workflow_increment_requests"):
                c.execute("UPDATE workflow_increment_requests SET state_hash=?", ("0" * 64,))
        else:
            row = {k: publication.record[k] for k in publication.fields}
            if damage == "descriptor":
                row["manifest_json"] = canonical_json({**MANIFEST, "stored_bytes": -1})
            elif damage == "source":
                source = {**publication.record["source"], "history_hash": "0" * 64}
                row["source_json"] = canonical_json(source)
            elif damage == "parent_reference":
                row["parent_ref_json"] = canonical_json(
                    {**publication.record["parent"], "manifest_hash": "0" * 64}
                )
            with corrupt_copy(c, publication.table):
                c.execute(
                    "UPDATE "
                    + publication.table
                    + " SET "
                    + ",".join(k + "=?" for k in row)
                    + ",state_hash=? WHERE publication_id=?",
                    (
                        *row.values(),
                        "0" * 64 if damage == "state_hash" else content_hash(row),
                        row["publication_id"],
                    ),
                )
    before = snapshot(old)
    with pytest.raises(IntegrityError):
        Database(old.path, strict_schema_contract=True)
    assert snapshot(old) == before


@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
def test_preflight_is_read_only_and_performs_no_archive_io(
    schemas: dict[int, Path], tmp_path: Path, state: str
) -> None:
    old = clone(schemas[116], tmp_path / "preflight.sqlite3")
    prepare(old, "increment", state)
    before = snapshot(old)
    with old.read_transaction() as c:
        changes = c.total_changes
        validate_publication_upgrade(c)
        assert c.total_changes == changes
    assert snapshot(old) == before


@pytest.mark.skipif(os.name != "posix", reason="native SQLite process-crash recovery")
@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
def test_process_exit_mid_trigger_migration_keeps_old_rows_and_old_runtime_openable(
    schemas: dict[int, Path], tmp_path: Path, state: str
) -> None:
    old = clone(schemas[116], tmp_path / "crash.sqlite3")
    prepare(old, "increment", state)
    before = snapshot(old)
    child = r"""
import os,sys
from unittest.mock import patch
from noyra.core import Database
from noyra.core.database import MIGRATIONS
original=Database._execute_sql_script
def interrupt(c,sql):
    original(c,sql)
    if sql==MIGRATIONS[117]:os._exit(88)
with patch.object(Database,'_execute_sql_script',staticmethod(interrupt)):
    Database(sys.argv[1],strict_schema_contract=True)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, str(old.path)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 88, result.stderr
    assert snapshot(old) == before
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 116):
        Database(old.path, strict_schema_contract=True)
    Database(old.path, strict_schema_contract=True)


def test_new_schema_is_trigger_only_and_all_old_migration_bytes_remain_frozen(
    schemas: dict[int, Path],
) -> None:
    assert _SCHEMA_STRUCTURE_FINGERPRINTS[117] == _SCHEMA_STRUCTURE_FINGERPRINTS[116]
    assert _SCHEMA_DDL_FINGERPRINTS[117] != _SCHEMA_DDL_FINGERPRINTS[116]
    contract = json.loads(
        (Path(__file__).parent / "contracts/database-definition-contract.json").read_text()
    )
    for version, digest in contract["migration_sha256"].items():
        assert (
            hashlib.sha256(database_module.MIGRATIONS[int(version)].encode()).hexdigest() == digest
        )
    for version, source in schemas.items():
        with Database(source, initialize=False).read_transaction() as c:
            assert Database.schema_ddl_fingerprint(c) == _SCHEMA_DDL_FINGERPRINTS[version]


@pytest.mark.parametrize("kind", ("export", "increment"))
def test_archived_receipt_preflight_uses_bound_hash_without_archive_reads(
    schemas: dict[int, Path], tmp_path: Path, kind: str
) -> None:
    from datetime import datetime, timedelta

    from noyra.core.archive import LocalArchiveProvider
    from noyra.core.event_archive import EventPayloadArchive

    old = clone(schemas[116], tmp_path / "archived.sqlite3")
    publication = prepare(old, kind, "accepted")

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            instant = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(instant.timestamp(), tz)

    with patch("noyra.core.event_archive.datetime", Future):
        assert (
            EventPayloadArchive(
                old, old.path.parent / "subject/cold", subject_id=SUBJECT
            ).archive_cold(SUBJECT, older_than_days=90)
            > 0
        )
    with old.read_transaction() as c:
        event = c.execute(
            "SELECT payload_archive_key FROM events WHERE event_id=?",
            (publication.record["receipt_event_id"],),
        ).fetchone()
        assert event[0] is not None
    before = snapshot(old)
    with patch.object(
        LocalArchiveProvider,
        "get_stored",
        side_effect=AssertionError("archive read under migration"),
    ):
        current = Database(old.path, strict_schema_contract=True)
    after = snapshot(current)
    for table in (publication.table, "events", "event_chain_roots", "workflow_checkpoints"):
        assert after["rows"][table] == before["rows"][table]
    # The regular reader still checks the actual encrypted receipt after migration.
    with current.read_transaction() as c:
        assert (
            publication.catalog.get(c, publication.record["publication_id"]) == publication.record
        )


@pytest.mark.parametrize("kind", ("export", "increment"))
def test_missing_old_acceptance_event_refuses_upgrade_and_restores_the_corrupt_copy(
    schemas: dict[int, Path], tmp_path: Path, kind: str
) -> None:
    old = clone(schemas[116], tmp_path / "missing-receipt.sqlite3")
    publication = prepare(old, kind, "accepted")
    with closing(sqlite3.connect(old.path)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=OFF")
        with corrupt_copy(c, "events"):
            c.execute(
                "DELETE FROM events WHERE event_id=?", (publication.record["receipt_event_id"],)
            )
        c.commit()
    before = snapshot(old)
    with pytest.raises(IntegrityError, match="receipt"):
        Database(old.path, strict_schema_contract=True)
    assert snapshot(old) == before


@pytest.mark.parametrize("kind", ("export", "increment"))
def test_old_inline_payload_cannot_be_replaced_while_retaining_its_expected_hash(
    schemas: dict[int, Path], tmp_path: Path, kind: str
) -> None:
    old = clone(schemas[116], tmp_path / "inline-receipt.sqlite3")
    publication = prepare(old, kind, "accepted")
    with old.transaction() as c, corrupt_copy(c, "events"):
        c.execute(
            "UPDATE events SET payload_json='{}' WHERE event_id=?",
            (publication.record["receipt_event_id"],),
        )
    before = snapshot(old)
    with pytest.raises(IntegrityError, match="inline"):
        Database(old.path, strict_schema_contract=True)
    assert snapshot(old) == before


@pytest.mark.parametrize("kind", ("export", "increment"))
def test_noncanonical_old_unknown_descriptor_survives_upgrade_and_promotes_without_reencoding(
    schemas: dict[int, Path], tmp_path: Path, kind: str
) -> None:
    old = clone(schemas[116], tmp_path / "noncanonical.sqlite3")
    publication = prepare(old, kind, "published_durability_unknown")
    raw = {key: publication.record[key] for key in publication.fields}
    raw["manifest_json"] = json.dumps(dict(reversed(list(MANIFEST.items()))), indent=2)
    with old.transaction() as c, corrupt_copy(c, publication.table):
        c.execute(
            "UPDATE "
            + publication.table
            + " SET manifest_json=?,state_hash=? WHERE publication_id=?",
            (raw["manifest_json"], content_hash(raw), raw["publication_id"]),
        )
    Database(old.path, strict_schema_contract=True)
    with old.transaction() as c:
        saved = publication.catalog.get(c, raw["publication_id"])
        assert saved is not None and saved["manifest_json"] == raw["manifest_json"]
        accepted = publication.catalog.finish(c, saved, MANIFEST, durable=True)
        assert (
            accepted["status"] == "accepted" and accepted["manifest_json"] == raw["manifest_json"]
        )


@pytest.mark.parametrize("wrong_field", ("event_type", "source", "privacy_level"))
def test_acceptance_guard_checks_bound_event_metadata_not_only_foreign_key(
    publication: Publication, wrong_field: str
) -> None:
    publication.finish(durable=False)
    record = publication.record
    prefix = "evt_wexport_accepted_" if publication.kind == "export" else "evt_wincrement_accepted_"
    identifier = prefix + (
        record["source_hash"] if publication.kind == "export" else record["publication_id"]
    )
    metadata = {
        "event_type": "workflow_" + publication.kind + "_accepted",
        "source": "workflow_" + publication.kind,
        "privacy_level": "private",
    }
    metadata[wrong_field] = "public" if wrong_field == "privacy_level" else "unrelated"
    with publication.database.transaction() as c:
        publication.catalog.events._append_connection(
            c,
            SUBJECT,
            metadata["event_type"],
            metadata["source"],
            {},
            privacy_level=metadata["privacy_level"],
            causal_parent_ids=(),
            occurred_at=None,
            event_id=identifier,
        )
    with pytest.raises(sqlite3.IntegrityError), publication.database.transaction() as c:
        c.execute(
            "UPDATE " + publication.table + " SET status='accepted',receipt_event_id=?",
            (identifier,),
        )
