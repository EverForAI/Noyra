from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core import Database
from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.core.workflow_bundle import inspect_source
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_d2b_workflow_bundle import database as database
from test_d2b_workflow_bundle import nothing, seed
from test_d2b_workflow_bundle import template as template

SUBJECT = "export-subject"
MANIFEST = {
    "name": "manifest.enc",
    "stored_bytes": 137,
    "stored_hash": "a" * 64,
    "plaintext_hash": "b" * 64,
}


def source(database: Database) -> dict[str, Any]:
    with database.read_snapshot() as c:
        return inspect_source(c, database, SUBJECT, "workflow", nothing)


def advance(database: Database, count: int = 1) -> dict[str, Any]:
    for index in range(count):
        with patch("noyra.autonomy.workflow.utc_now", return_value="2026-11-07T12:00:00+00:00"):
            DurableWorkflowStore(database).checkpoint(
                SUBJECT, "workflow", "fixture", "running", {"n": index}, reason="Synthetic suffix"
            )
    return source(database)


@pytest.fixture
def metadata(database: Database, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    seed(database, 3)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(database, SUBJECT)
    catalog = WorkflowExportCatalog(database, SUBJECT)
    original = source(database)
    with database.transaction() as c:
        base = catalog.prepare(c, original, content_hash("base-fixture"), ring)
        base = catalog.finish(c, base, MANIFEST, durable=True)
    return database, WorkflowIncrementCatalog(database, SUBJECT), ring, base


def prepare(
    metadata: tuple[Any, ...],
    parent: str | None = None,
    key: str = "request-one",
    original: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db, catalog, ring, base = metadata
    original = original or source(db)
    with db.transaction() as c:
        return dict(
            catalog.prepare(c, original, parent or base["publication_id"], content_hash(key), ring)
        )


def accepted(
    metadata: tuple[Any, ...], parent: str | None = None, key: str = "request-one"
) -> dict[str, Any]:
    record = prepare(metadata, parent, key)
    db, catalog, _ring, _base = metadata
    with db.transaction() as c:
        return dict(catalog.finish(c, record, MANIFEST, durable=True))


def test_increment_metadata_keeps_exact_parent_and_versioned_source(
    metadata: tuple[Any, ...],
) -> None:
    db, catalog, _ring, base = metadata
    original = advance(db, 2)
    prepared = prepare(metadata, original=original)
    assert prepared["publication_id"].startswith("wdelta_")
    assert prepared["status"] == "prepared" and prepared["manifest"] is None
    assert prepared["first_version"] == 4 and prepared["last_version"] == 5
    assert prepared["chain_depth"] == 1
    assert prepared["parent"]["publication_id"] == base["publication_id"]
    assert prepared["parent"]["acceptance_event_id"] == base["receipt_event_id"]
    assert prepared["parent"]["source_json_hash"] == content_hash(base["source"])
    with db.read_transaction() as c:
        assert catalog.get(c, prepared["publication_id"]) == prepared
        assert catalog.verify_requests(c) == 1
        assert c.total_changes == 0
        assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_logical_id_survives_unrelated_event_head_and_request_keys_pin_original(
    metadata: tuple[Any, ...],
) -> None:
    db, catalog, _ring, base = metadata
    initial = advance(db)
    first = prepare(metadata, original=initial)
    second = prepare(metadata, key="another-key")
    assert first == second and first["source"]["event_head"] == initial["event_head"]
    completed = accepted(metadata)
    advance(db)
    with db.read_transaction() as c:
        restored = catalog.requested(
            c, content_hash("request-one"), base["publication_id"], "workflow", 4
        )
        assert restored == completed
    for parent, workflow, version in [
        (base["publication_id"], "workflow", 5),
        (completed["publication_id"], "workflow", 4),
        (base["publication_id"], "other", 4),
    ]:
        with db.transaction() as c, pytest.raises(ValueError, match="another intent"):
            catalog.requested(c, content_hash("request-one"), parent, workflow, version)
    with pytest.raises(ValueError, match="another intent"):
        prepare(metadata)


def test_same_version_rehashed_source_cannot_retarget_a_prepared_request(
    metadata: tuple[Any, ...],
) -> None:
    db, _catalog, _ring, _base = metadata
    advance(db)
    original = prepare(metadata)
    changed = {**source(db), "history_hash": "0" * 64}
    with pytest.raises(ValueError, match="different frozen source"):
        prepare(metadata, original=changed)
    assert prepare(metadata)["publication_id"] == original["publication_id"]


@pytest.mark.parametrize("durable", [False, True])
def test_terminal_metadata_receipts_are_atomic_idempotent_and_manifest_is_frozen(
    metadata: tuple[Any, ...],
    durable: bool,
) -> None:
    db, catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.transaction() as c:
        final = catalog.finish(c, record, MANIFEST, durable=durable)
    assert final["status"] == ("accepted" if durable else "published_durability_unknown")
    with db.transaction() as c:
        assert catalog.finish(c, final, MANIFEST, durable=durable) == final
    with db.transaction() as c, pytest.raises(IntegrityError, match="cannot be replaced"):
        catalog.finish(c, final, {**MANIFEST, "stored_hash": "c" * 64}, durable=True)
    if not durable:
        with db.transaction() as c:
            final = catalog.finish(c, final, MANIFEST, durable=True)
    with db.read_transaction() as c:
        assert catalog.get(c, record["publication_id"]) == final
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
            ).fetchone()[0]
            == 1
        )


def test_acceptance_rollback_and_preparation_rollback_leave_no_partial_records(
    metadata: tuple[Any, ...],
) -> None:
    db, catalog, ring, base = metadata
    original = advance(db)
    with pytest.raises(OSError), db.transaction() as c:
        catalog.prepare(c, original, base["publication_id"], content_hash("failed-request"), ring)
        raise OSError("synthetic preparation fault")
    with db.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_increment_publications").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM workflow_increment_requests").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM events WHERE source='workflow_increment'").fetchone()[0]
            == 0
        )
    record = prepare(metadata)
    with pytest.raises(OSError), db.transaction() as c:
        catalog.finish(c, record, MANIFEST, durable=True)
        raise OSError("synthetic audit or final transaction fault")
    with db.read_transaction() as c:
        assert catalog.get(c, record["publication_id"])["status"] == "prepared"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
            ).fetchone()[0]
            == 0
        )
    assert accepted(metadata)["status"] == "accepted"


@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown"])
def test_only_independently_accepted_increment_may_be_a_parent(
    metadata: tuple[Any, ...],
    state: str,
) -> None:
    db, catalog, _ring, _base = metadata
    advance(db)
    parent = prepare(metadata)
    if state != "prepared":
        with db.transaction() as c:
            parent = catalog.finish(c, parent, MANIFEST, durable=False)
    advance(db)
    with pytest.raises(IntegrityError, match="not accepted"):
        prepare(metadata, parent["publication_id"], "next-key")
    with db.transaction() as c:
        parent = catalog.finish(c, parent, MANIFEST, durable=True)
    child = accepted(metadata, parent["publication_id"], "next-key")
    assert child["chain_depth"] == 2 and child["first_version"] == 5
    with db.read_transaction() as c:
        assert catalog.get(c, child["publication_id"]) == child


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_id", "other"),
        ("workflow_id", "different"),
        ("genesis_hash", "0" * 64),
        ("records", 3),
    ],
)
def test_scope_and_version_conflicts_do_not_prepare_records(
    metadata: tuple[Any, ...],
    field: str,
    value: Any,
) -> None:
    db, _catalog, _ring, _base = metadata
    original = advance(db)
    with pytest.raises((ValueError, IntegrityError)):
        prepare(metadata, original={**original, field: value})
    with db.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_increment_publications").fetchone()[0] == 0


def test_no_file_writer_route_and_new_privacy_intent_prevents_preparation_or_promotion(
    metadata: tuple[Any, ...],
) -> None:
    from noyra.core.workflow_export_files import WorkflowArtifactStore
    from noyra.interaction.conversations import ConversationStore

    db, catalog, _ring, _base = metadata
    advance(db)
    with patch.object(
        WorkflowArtifactStore, "put", side_effect=AssertionError("metadata wrote file")
    ):
        record = prepare(metadata)
    window = ConversationStore(db).create(SUBJECT, "Synthetic privacy fixture")
    with db.transaction() as c:
        c.execute(
            "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
            (window["conversation_id"],),
        )
    with pytest.raises(OperationInvalidated, match="privacy"):
        prepare(metadata, key="another-key")
    with db.transaction() as c, pytest.raises(OperationInvalidated, match="privacy"):
        catalog.finish(c, record, MANIFEST, durable=True)
    with db.read_transaction() as c:
        assert catalog.get(c, record["publication_id"])["status"] == "prepared"


def test_chain_depth_is_bounded_and_cycles_cannot_be_created(metadata: tuple[Any, ...]) -> None:
    db, _catalog, _ring, base = metadata
    parent = base["publication_id"]
    for index in range(15):
        advance(db)
        parent = accepted(metadata, parent, "chain-key-" + str(index))["publication_id"]
    advance(db)
    with pytest.raises(IntegrityError, match="bounds"):
        prepare(metadata, parent, "too-deep")


@pytest.mark.parametrize(
    "patches",
    [
        {"stored_bytes": True},
        {"stored_hash": "broken"},
        {"name": "../manifest.enc"},
        {"stored_bytes": 1024 * 1024 + 38},
    ],
)
def test_invalid_manifest_descriptor_never_advances_metadata(
    metadata: tuple[Any, ...], patches: dict[str, Any]
) -> None:
    db, catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.transaction() as c, pytest.raises(IntegrityError):
        catalog.finish(c, record, {**MANIFEST, **patches}, durable=True)
    with db.read_transaction() as c:
        assert catalog.get(c, record["publication_id"])["status"] == "prepared"
