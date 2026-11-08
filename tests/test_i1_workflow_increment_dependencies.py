from __future__ import annotations

import hashlib
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.archive import ArchiveKeyMaterial, ArchiveKeyring
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.operator_controls import OperatorControlService
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_i1_workflow_increment_catalog import MANIFEST, SUBJECT, accepted, advance, prepare
from test_i1_workflow_increment_catalog import database as database
from test_i1_workflow_increment_catalog import metadata as metadata
from test_i1_workflow_increment_catalog import template as template


def rotated(old: ArchiveKeyring) -> ArchiveKeyring:
    key = bytes(reversed(range(32)))
    active = ArchiveKeyMaterial("increment-key", key, hashlib.sha256(key).hexdigest(), "active")
    retired = ArchiveKeyMaterial(
        old.active.key_id, old.active.key, old.active.fingerprint, "retired"
    )
    return ArchiveKeyring(
        generation=2,
        active_key_id=active.key_id,
        legacy_key_id=retired.key_id,
        keys=(retired, active),
    )


@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown", "accepted"])
def test_all_increment_states_keep_parent_and_child_keys_referenced(
    metadata: tuple[Any, ...],
    state: str,
) -> None:
    db, catalog, old, base = metadata
    ring = rotated(old)
    original = advance(db)
    with db.transaction() as c, pytest.raises(IntegrityError, match="recorded first"):
        catalog.prepare(c, original, base["publication_id"], content_hash("unrecorded"), ring)
    ring.record_revision(db, SUBJECT)
    original = advance(db)
    with db.transaction() as c:
        record = catalog.prepare(c, original, base["publication_id"], content_hash("rotated"), ring)
        if state != "prepared":
            record = catalog.finish(c, record, MANIFEST, durable=state == "accepted")
    for retained in [
        (ring.active,),
        (ArchiveKeyMaterial(old.active.key_id, old.active.key, old.active.fingerprint, "active"),),
    ]:
        active = retained[0]
        missing = ArchiveKeyring(
            generation=3, active_key_id=active.key_id, legacy_key_id=None, keys=retained
        )
        with pytest.raises(ArchiveKeyUnavailableError):
            missing.validate_revision(db, SUBJECT)
    with pytest.raises(ArchiveKeyUnavailableError, match="rolled back"):
        old.validate_revision(db, SUBJECT)
    ring.validate_revision(db, SUBJECT)
    with db.read_transaction() as c:
        assert catalog.get(c, record["publication_id"])["status"] == state


def test_retry_keeps_original_key_and_source_after_rotation(metadata: tuple[Any, ...]) -> None:
    db, catalog, old, base = metadata
    original = advance(db)
    record = prepare(metadata)
    ring = rotated(old)
    ring.record_revision(db, SUBJECT)
    with db.transaction() as c:
        retry = catalog.prepare(
            c, original, base["publication_id"], content_hash("request-one"), ring
        )
        another = catalog.prepare(
            c, original, base["publication_id"], content_hash("same-source"), ring
        )
    assert retry == another == record
    assert retry["encryption_key_id"] == old.active.key_id


def test_increment_health_integrity_preservation_and_legacy_privacy_are_connected(
    metadata: tuple[Any, ...],
) -> None:
    from noyra.core.lifecycle_inventory import TABLE_DOMAINS
    from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired

    db, _catalog, _ring, _base = metadata
    advance(db)
    accepted(metadata)
    controls = OperatorControlService(SimpleNamespace(database=db, subject_id=SUBJECT))
    assert controls._archive_key_health()["references"] == 2
    assert "workflow_increment_publications" in TABLE_DOMAINS
    assert "workflow_increment_requests" in TABLE_DOMAINS
    with db.read_transaction() as c, pytest.raises(ErasureReviewRequired, match="legacy_export"):
        ConversationErasure._review_exports(c, SUBJECT)
    report = IntegrityRegistry().run(
        db,
        SUBJECT,
        db.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("operations.retention",),
    )
    assert not report.findings
    assert report.checks[0].details["workflow_increments"] == 1
    assert report.checks[0].details["workflow_increment_requests"] == 1


def test_runtime_export_keeps_subject_owned_increment_and_request_rows(
    metadata: tuple[Any, ...],
) -> None:
    import json

    db, _catalog, _ring, _base = metadata
    advance(db)
    record = accepted(metadata)
    IdentityStore(db).ensure("other-subject", content_hash("other-genesis"))
    for subject, expected in [(SUBJECT, 1), ("other-subject", 0)]:
        artifact = RuntimeLogExporter(db).export(subject, actor="test")
        with ZipFile(BytesIO(artifact.content)) as archive:
            rows = [
                json.loads(line)
                for line in archive.read(
                    "tables/workflow_increment_publications.jsonl"
                ).splitlines()
            ]
            requests = [
                json.loads(line)
                for line in archive.read("tables/workflow_increment_requests.jsonl").splitlines()
            ]
            assert len(rows) == len(requests) == expected
            if expected:
                assert (
                    rows[0]["publication_id"]
                    == requests[0]["publication_id"]
                    == record["publication_id"]
                )


def test_archived_receipts_and_new_database_object_keep_request_resolution(
    metadata: tuple[Any, ...],
) -> None:
    from datetime import datetime, timedelta

    from noyra.core.event_archive import EventPayloadArchive

    db, _catalog, _ring, base = metadata
    advance(db)
    record = accepted(metadata)

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            instant = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(instant.timestamp(), tz)

    with patch("noyra.core.event_archive.datetime", Future):
        assert (
            EventPayloadArchive(
                db, db.path.parent / "subject/cold", subject_id=SUBJECT
            ).archive_cold(SUBJECT, older_than_days=90)
            > 0
        )
    reopened = Database(db.path, strict_schema_contract=True)
    catalog = WorkflowIncrementCatalog(reopened, SUBJECT)
    with reopened.read_transaction() as c:
        assert catalog.get(c, record["publication_id"]) == record
        assert (
            catalog.requested(c, content_hash("request-one"), base["publication_id"], "workflow", 4)
            == record
        )
        assert catalog.verify_requests(c) == 1


def test_unknown_call_and_fee_reservations_survive_metadata_transitions(
    metadata: tuple[Any, ...],
) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    db, _catalog, _ring, _base = metadata
    ledger = ModelLedger(db)
    call, _ = ledger.prepare_call(
        SUBJECT, "fixture", "fixture", "i1-unknown", "f" * 64, "i1-unknown-key"
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=11,
        reserved_output_tokens=22,
        reserved_cost_microusd=33,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.recover_interrupted(SUBJECT)
    advance(db)
    tables = ("model_calls", "model_attempts", "workflow_checkpoints")
    with db.read_transaction() as c:
        before = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in tables
        }
    accepted(metadata)
    with db.read_transaction() as c:
        after = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in tables
        }
    assert before == after
    assert after["model_calls"][0]["status"] == "unknown"
    assert after["model_attempts"][0]["reserved_cost_microusd"] == 33
