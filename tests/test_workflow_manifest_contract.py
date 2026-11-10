from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from noyra.core.workflow_bundle import MAX_MANIFEST_BYTES, inspect_source
from noyra.core.workflow_export_catalog import FIELDS as BASE_FIELDS
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_increment_catalog import FIELDS as INCREMENT_FIELDS
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_d2b_workflow_bundle import database as database
from test_d2b_workflow_bundle import nothing, seed
from test_d2b_workflow_bundle import template as template
from test_i1_workflow_increment_catalog import advance

SUBJECT = "export-subject"
MANIFEST = {
    "name": "manifest.enc",
    "stored_bytes": 137,
    "stored_hash": "a" * 64,
    "plaintext_hash": "b" * 64,
}


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

    def finish(self, manifest: Any = None, *, durable: Any = True) -> dict[str, Any]:
        with self.database.transaction() as c:
            return self.catalog.finish(
                c, self.record, MANIFEST if manifest is None else manifest, durable=durable
            )

    def state(self) -> str:
        with self.database.read_transaction() as c:
            record = self.catalog.get(c, self.record["publication_id"])
        assert record is not None
        return str(record["status"])

    def snapshot(self) -> str:
        with self.database.read_transaction() as c:
            return content_hash(
                {
                    table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
                    for table in (
                        self.table,
                        "events",
                        "event_chain_roots",
                        "audit_records",
                        "workflow_checkpoints",
                        "model_calls",
                        "model_attempts",
                    )
                }
            )


@pytest.fixture(params=("export", "increment"))
def publication(
    database: Database, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Publication:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    seed(database, 3)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(database, SUBJECT)
    with database.read_snapshot() as c:
        source = inspect_source(c, database, SUBJECT, "workflow", nothing)
    base = WorkflowExportCatalog(database, SUBJECT)
    with database.transaction() as c:
        record = base.prepare(c, source, content_hash("base-manifest-contract"), ring)
    if request.param == "export":
        return Publication(database, base, record, "export")
    with database.transaction() as c:
        parent = base.finish(c, record, MANIFEST, durable=True)
    source = advance(database)
    increment = WorkflowIncrementCatalog(database, SUBJECT)
    with database.transaction() as c:
        record = increment.prepare(
            c, source, parent["publication_id"], content_hash("increment-manifest-contract"), ring
        )
    return Publication(database, increment, record, "increment")


INVALID_PATCHES: tuple[dict[str, Any], ...] = (
    {"name": "segment-000000.enc"},
    {"name": 1},
    {"stored_bytes": -1},
    {"stored_bytes": 36},
    {"stored_bytes": True},
    {"stored_bytes": 137.0},
    {"stored_bytes": "137"},
    {"stored_bytes": MAX_MANIFEST_BYTES + 38},
    {"stored_hash": "broken"},
    {"stored_hash": "a" * 63},
    {"stored_hash": "A" * 64},
    {"stored_hash": 42},
    {"stored_hash": "a" * 64 + "\n"},
    {"plaintext_hash": None},
    {"plaintext_hash": "g" * 64},
    {"extra": "not-a-manifest-field"},
)


@pytest.mark.parametrize("patches", INVALID_PATCHES)
@pytest.mark.parametrize("durable", (False, True))
def test_invalid_manifest_cannot_publish_or_append_acceptance(
    publication: Publication, patches: dict[str, Any], durable: bool
) -> None:
    before = publication.snapshot()
    with pytest.raises(IntegrityError):
        publication.finish({**MANIFEST, **patches}, durable=durable)
    assert publication.state() == "prepared" and publication.snapshot() == before


@pytest.mark.parametrize("durable", (None, 0, 1, "true", 1.0))
def test_durability_requires_an_actual_boolean_before_any_write(
    publication: Publication, durable: Any
) -> None:
    before = publication.snapshot()
    with pytest.raises(ValueError, match="durability"):
        publication.finish(durable=durable)
    assert publication.snapshot() == before


@pytest.mark.parametrize("accepted", (False, True))
@pytest.mark.parametrize("durable", (False, True))
def test_every_published_retry_requires_the_original_manifest(
    publication: Publication, accepted: bool, durable: bool
) -> None:
    publication.finish(durable=accepted)
    before = publication.snapshot()
    with pytest.raises(IntegrityError, match="cannot be replaced"):
        publication.finish({**MANIFEST, "stored_hash": "c" * 64}, durable=durable)
    assert publication.snapshot() == before


@pytest.mark.parametrize(
    "field", ("source", "encryption_key_id", "encryption_key_fingerprint", "key_generation")
)
def test_stale_or_retargeted_preparation_cannot_authorize_acceptance(
    publication: Publication, field: str
) -> None:
    before = publication.snapshot()
    changed = dict(publication.record)
    if field == "source":
        changed[field] = {**changed[field], "history_hash": "c" * 64}
    elif field == "key_generation":
        changed[field] += 1
    elif field == "encryption_key_fingerprint":
        changed[field] = "c" * 64
    else:
        changed[field] += "-other"
    with publication.database.transaction() as c, pytest.raises(IntegrityError):
        publication.catalog.finish(c, changed, MANIFEST, durable=True)
    assert publication.snapshot() == before


@pytest.mark.parametrize("size", (37, MAX_MANIFEST_BYTES + 37))
def test_existing_descriptor_byte_boundaries_remain_compatible(
    publication: Publication, size: int
) -> None:
    result = publication.finish({**MANIFEST, "stored_bytes": size})
    assert result["status"] == "accepted"


def test_noncanonical_frozen_json_can_promote_without_rewriting_descriptor(
    publication: Publication,
) -> None:
    # Schema 115 permits this historical JSON layout. Increment metadata already
    # freezes the raw JSON string; construct that legacy layout at preparation.
    descriptor_text = json.dumps(dict(reversed(list(MANIFEST.items()))), indent=2)
    original = publication.record
    raw = {key: original[key] for key in publication.fields}
    raw.update(status="published_durability_unknown", manifest_json=descriptor_text)
    with publication.database.transaction() as c:
        c.execute(
            "UPDATE "
            + publication.table
            + " SET status=?,manifest_json=?,state_hash=? WHERE publication_id=?",
            (raw["status"], descriptor_text, content_hash(raw), original["publication_id"]),
        )
        saved = publication.catalog.get(c, original["publication_id"])
    assert saved is not None
    publication.record = saved
    result = publication.finish()
    assert result["status"] == "accepted" and result["manifest_json"] == descriptor_text
    before = publication.snapshot()
    with publication.database.transaction() as c:
        changes = c.total_changes
        repeated = publication.catalog.finish(
            c, result, dict(reversed(list(MANIFEST.items()))), durable=True
        )
        assert c.total_changes == changes
    assert repeated == result and publication.snapshot() == before


@pytest.mark.parametrize("accepted", (False, True))
def test_exact_retries_are_read_only_even_with_a_stale_prepared_handle(
    publication: Publication, accepted: bool
) -> None:
    first = publication.finish(durable=accepted)
    before = publication.snapshot()
    with publication.database.transaction() as c:
        changes = c.total_changes
        again = publication.catalog.finish(c, publication.record, MANIFEST, durable=False)
        assert c.total_changes == changes
    assert again == first and publication.snapshot() == before


@pytest.mark.parametrize("accepted", (False, True))
def test_legacy_base_with_rehashed_invalid_descriptor_is_rejected_on_read(
    database: Database, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    seed(database, 3)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(database, SUBJECT)
    catalog = WorkflowExportCatalog(database, SUBJECT)
    with database.read_snapshot() as c:
        source = inspect_source(c, database, SUBJECT, "workflow", nothing)
    with database.transaction() as c:
        prepared = catalog.prepare(c, source, content_hash("legacy-invalid-contract"), ring)
        bad = {**MANIFEST, "stored_bytes": -1}
        raw = {key: prepared[key] for key in BASE_FIELDS}
        raw.update(
            status="accepted" if accepted else "published_durability_unknown",
            manifest_json=canonical_json(bad),
        )
        if accepted:
            raw["receipt_event_id"] = "evt_wexport_accepted_" + raw["source_hash"]
            catalog.events._append_connection(
                c,
                SUBJECT,
                "workflow_export_accepted",
                "workflow_export",
                {
                    **catalog._binding(prepared),
                    "manifest": bad,
                    "scope": "workflow_history_and_clock_receipts_only",
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=None,
                event_id=raw["receipt_event_id"],
            )
        # This is a pre-existing schema-115/116 row, not a call through finish.
        c.execute(
            "UPDATE workflow_export_publications SET status=?,manifest_json=?,"
            "receipt_event_id=?,state_hash=? WHERE publication_id=?",
            (
                raw["status"],
                raw["manifest_json"],
                raw["receipt_event_id"],
                content_hash(raw),
                raw["publication_id"],
            ),
        )
    with database.read_transaction() as c:
        before = c.total_changes
        with pytest.raises(IntegrityError, match="manifest"):
            catalog.get(c, prepared["publication_id"])
        assert c.total_changes == before


@pytest.mark.parametrize("bad", (None, [], "manifest.enc", {}, {"name": "manifest.enc"}))
def test_wrong_container_or_incomplete_manifest_is_rejected_before_writes(
    publication: Publication, bad: Any
) -> None:
    before = publication.snapshot()
    with publication.database.transaction() as c, pytest.raises(IntegrityError):
        publication.catalog.finish(c, publication.record, bad, durable=True)
    assert publication.snapshot() == before


@pytest.fixture(scope="module")
def schema115_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 115):
        database = Database(
            tmp_path_factory.mktemp("schema115") / "legacy.sqlite3", strict_schema_contract=True
        )
    IdentityStore(database).ensure(SUBJECT, content_hash("export-genesis"))
    return database.path


@pytest.mark.parametrize("version", (115, 116))
@pytest.mark.parametrize("state", ("prepared", "published_durability_unknown", "accepted"))
def test_valid_legacy_base_records_remain_readable_without_rewriting_source_or_receipts(
    template: Path,
    schema115_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    state: str,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    selected = schema115_template if version == 115 else template
    with closing(sqlite3.connect(selected)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    database = Database(path, initialize=False, strict_schema_contract=True)
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    seed(database, 3)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(database, SUBJECT)
    with database.read_snapshot() as c:
        frozen = inspect_source(c, database, SUBJECT, "workflow", nothing)
    assert frozen["schema_version"] == version
    catalog = WorkflowExportCatalog(database, SUBJECT)
    with database.transaction() as c:
        prepared = catalog.prepare(c, frozen, content_hash("legacy-valid-contract"), ring)
        saved = (
            prepared
            if state == "prepared"
            else catalog.finish(c, prepared, MANIFEST, durable=state == "accepted")
        )
    publication = Publication(database, catalog, prepared, "export")
    before = publication.snapshot()
    reopened = Database(path, initialize=False, strict_schema_contract=True)
    with reopened.read_transaction() as c:
        changes = c.total_changes
        assert WorkflowExportCatalog(reopened, SUBJECT).get(c, saved["publication_id"]) == saved
        assert c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(version)
        assert c.total_changes == changes
    assert publication.snapshot() == before
