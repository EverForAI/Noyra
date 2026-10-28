from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition.settings import CognitionSettings
from noyra.cognition.social import RelationshipSocialCognition
from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.relationship_identity import backfill_legacy_bindings, context_rows, verify_binding
from noyra.core.retention import RETENTION_REGISTRY
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash
from noyra.interaction.integrity import InteractionIntegrity
from noyra.mind import MindEngine, RelationshipStore
from noyra.mind.errors import MindStateConflictError
from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger
from support.relationship_provenance import open_readonly, snapshot

SUBJECT = "Noyra-r01-validation"
OTHER = "Noyra-r01-foreign"
FIXTURE_ROOT = Path(__file__).parent / "fixtures/historical"
METADATA = json.loads((FIXTURE_ROOT / "r01-schema-v106.json").read_text(encoding="utf-8"))


def legacy_copy(tmp_path: Path) -> Path:
    archive = (FIXTURE_ROOT / METADATA["archive"]).read_bytes()
    assert hashlib.sha256(archive).hexdigest() == METADATA["archive_sha256"]
    raw = gzip.decompress(archive)
    assert hashlib.sha256(raw).hexdigest() == METADATA["database_sha256"]
    path = tmp_path / "legacy.sqlite3"
    path.write_bytes(raw)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "new.sqlite3")
    for subject in (SUBJECT, OTHER):
        IdentityStore(database).ensure(subject, content_hash(subject))
    return database


def new_relationship(db: Database) -> Any:
    event = EventStore(db).append(SUBJECT, "observation", "fixture", {})
    return RelationshipStore(db).ensure(
        SUBJECT, "human", "alice", "same nickname", source_event_ids=(event.event_id,)
    )


def social(db: Database) -> RelationshipSocialCognition:
    return RelationshipSocialCognition(
        db,
        SUBJECT,
        ModelGateway(
            FakeProvider([]),
            ModelLedger(db),
            model="fixture",
            limits=BudgetLimits(10, 100000, 100000, 100000),
        ),
        CognitionSettings(),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity_key", "bob"),
        ("entity_type", "agent"),
        ("subject_id", OTHER),
        ("created_at", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_identity_update_is_rejected_and_bypassed_guard_is_detected(
    db: Database, field: str, value: str
) -> None:
    row = new_relationship(db)
    with (
        pytest.raises(sqlite3.IntegrityError, match="identity is immutable"),
        db.transaction() as c,
    ):
        c.execute(
            f"UPDATE relationships SET {field}=? WHERE relationship_id=?",
            (value, row.relationship_id),
        )
    assert RelationshipStore(db).get(row.relationship_id).identity_origin == "created"
    with db.transaction() as c:
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            f"UPDATE relationships SET {field}=? WHERE relationship_id=?",
            (value, row.relationship_id),
        )
    with pytest.raises(IntegrityError):
        RelationshipStore(db).get(row.relationship_id)
    with pytest.raises(IntegrityError):
        MindEngine(db).verify_integrity(SUBJECT)
    with db.connection() as c, pytest.raises(IntegrityError):
        context_rows(
            c,
            [{"relationship_id": row.relationship_id}],
            OTHER if field == "subject_id" else SUBJECT,
        )


@pytest.mark.parametrize("failure", ["missing", "hash", "identity", "blob"])
def test_binding_cannot_be_deleted_or_forged_and_never_repairs_at_reopen(
    db: Database, failure: str
) -> None:
    row = new_relationship(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), db.transaction() as c:
        c.execute("DELETE FROM relationship_identity_bindings")
    with db.transaction() as c:
        if failure == "missing":
            c.execute("DROP TRIGGER prevent_relationship_binding_delete")
            c.execute("DELETE FROM relationship_identity_bindings")
        else:
            c.execute("DROP TRIGGER prevent_relationship_binding_update")
            if failure == "hash":
                c.execute("UPDATE relationship_identity_bindings SET state_hash='broken'")
            elif failure == "identity":
                c.execute("UPDATE relationship_identity_bindings SET entity_key='bob'")
            else:
                c.execute(
                    "UPDATE relationship_identity_bindings SET recorded_at=?",
                    (sqlite3.Binary(b"bad"),),
                )
    # Startup construction does not silently re-attest a current-schema record.
    Database(db.path)
    with pytest.raises(IntegrityError):
        RelationshipStore(db).get(row.relationship_id)


def test_legacy_upgrade_preserves_every_old_value_and_is_idempotent(tmp_path: Path) -> None:
    path = legacy_copy(tmp_path)
    with open_readonly(path) as c:
        before = snapshot(c)
    database = Database(path)
    database.assert_schema_contract()
    with open_readonly(path) as c:
        assert snapshot(c) == before
        binding = dict(c.execute("SELECT * FROM relationship_identity_bindings").fetchone())
        assert binding["origin"] == "legacy_observed"
        assert binding["review_reasons_json"] == "[]"
        assert (
            verify_binding(c, c.execute("SELECT * FROM relationships").fetchone())[0]
            == "legacy_observed"
        )
    Database(path)
    with open_readonly(path) as c:
        assert dict(c.execute("SELECT * FROM relationship_identity_bindings").fetchone()) == binding
        assert snapshot(c) == before
    row = RelationshipStore(database).get(METADATA["relationship_id"])
    assert row.trust == 0.7 and row.familiarity == 0.8
    assert row.identity_status == "consistent"
    with database.connection() as c:
        summaries = context_rows(c, [{"relationship_id": row.relationship_id}], SUBJECT)
    assert summaries[0]["identity_origin"] == "legacy_observed"
    assert social(database)._context() is not None


@pytest.mark.parametrize("conflict", ["social_key", "social_hash", "foreign_source"])
def test_confirmed_legacy_conflicts_are_quarantined_without_moving_weights(
    tmp_path: Path, conflict: str
) -> None:
    path = legacy_copy(tmp_path)
    with closing(sqlite3.connect(path)) as c:
        if conflict == "social_key":
            c.execute("UPDATE relationships SET entity_key='changed-before-upgrade'")
        elif conflict == "social_hash":
            c.execute("DROP TRIGGER prevent_relationship_social_update")
            c.execute("UPDATE relationship_social_runs SET state_hash='broken'")
        else:
            c.execute("DROP TRIGGER prevent_relationship_revision_update")
            c.execute(
                "UPDATE relationship_revisions SET source_event_ids_json=? WHERE revision_number=1",
                (json.dumps(["evt-absent"]),),
            )
        c.commit()
    with open_readonly(path) as c:
        before = snapshot(c)
    database = Database(path)
    row = RelationshipStore(database).get(METADATA["relationship_id"])
    assert row.identity_status == "review_required"
    assert row.trust == 0.7
    with database.connection() as c:
        assert context_rows(c, [{"relationship_id": row.relationship_id}], SUBJECT) == []
    assert social(database)._context() is None
    with pytest.raises(MindStateConflictError, match="requires review"):
        RelationshipStore(database).revise(
            row.relationship_id,
            trust=0.9,
            affinity=0.9,
            conflict=0,
            familiarity=0.9,
            boundaries={},
            reason="must not inherit",
            source_event_ids=(METADATA["anchor_event_id"],),
        )
    with open_readonly(path) as c:
        assert snapshot(c) == before
    # Known key mismatch is recorded/quarantined, not misreported as a new
    # global integrity incident. Other malformed history remains detectable.
    if conflict == "social_key":
        assert MindEngine(database).verify_integrity(SUBJECT)["relationships"] == 1
        with database.connection() as c:
            InteractionIntegrity._verify_social_run(
                c, SUBJECT, c.execute("SELECT * FROM relationship_social_runs").fetchone()
            )


@pytest.mark.parametrize("phase", ["backfill", "schema_marker"])
def test_migration_failure_restores_complete_old_database_and_retry_succeeds(
    tmp_path: Path, phase: str
) -> None:
    path = legacy_copy(tmp_path)
    with closing(sqlite3.connect(path)) as original_db:
        before = tuple(original_db.iterdump())
    import noyra.core.database as module

    patcher: Any
    if phase == "backfill":
        original = backfill_legacy_bindings

        def fail(connection: Any) -> None:
            original(connection)
            raise RuntimeError("injected after binding write")

        patcher = patch.object(module, "backfill_legacy_bindings", fail)
    else:
        original_marker = Database._set_schema_version

        def mark(self: Database, connection: Any, version: int) -> None:
            if version == 107:
                raise RuntimeError("injected schema marker failure")
            original_marker(self, connection, version)

        patcher = patch.object(Database, "_set_schema_version", mark)
    with patcher, pytest.raises(RuntimeError, match="injected"):
        Database(path)
    with closing(sqlite3.connect(path)) as restored_db:
        assert tuple(restored_db.iterdump()) == before
    with open_readonly(path) as c:
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "106"
        )
        assert (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='relationship_identity_bindings'"
            ).fetchone()
            is None
        )
    assert (
        RelationshipStore(Database(path)).get(METADATA["relationship_id"]).identity_origin
        == "legacy_observed"
    )


def test_new_creation_is_atomic_and_ordinary_revision_preserves_binding(db: Database) -> None:
    event = EventStore(db).append(SUBJECT, "observation", "fixture", {})
    with (
        patch("noyra.mind.relationship.insert_binding", side_effect=RuntimeError("injected")),
        pytest.raises(RuntimeError),
    ):
        RelationshipStore(db).ensure(
            SUBJECT, "human", "alice", "same nickname", source_event_ids=(event.event_id,)
        )
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM relationship_revisions").fetchone()[0] == 0
    row = RelationshipStore(db).ensure(
        SUBJECT, "human", "alice", "same nickname", source_event_ids=(event.event_id,)
    )
    with db.connection() as c:
        before = dict(c.execute("SELECT * FROM relationship_identity_bindings").fetchone())
    updated = RelationshipStore(db).revise(
        row.relationship_id,
        trust=0.8,
        affinity=0.6,
        conflict=0.1,
        familiarity=0.7,
        boundaries={"no_contact": True},
        reason="normal",
        source_event_ids=(event.event_id,),
    )
    assert updated.boundaries["no_contact"]
    with db.connection() as c:
        assert dict(c.execute("SELECT * FROM relationship_identity_bindings").fetchone()) == before
    assert MindEngine(db).verify_integrity(SUBJECT)["relationships"] == 1


def test_backup_export_retention_and_private_explanation_include_binding(tmp_path: Path) -> None:
    database = Database(legacy_copy(tmp_path))
    artifact = RuntimeLogExporter(database).export(SUBJECT, actor="fixture")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        names = archive.namelist()
        file = next(n for n in names if n.endswith("relationship_identity_bindings.jsonl"))
        data = [json.loads(line) for line in archive.read(file).decode().splitlines()]
        assert len(data) == 1 and data[0]["subject_id"] == SUBJECT
        assert data[0]["origin"] == "legacy_observed"
    spec = next(s for s in RETENTION_REGISTRY if s.table == "relationship_identity_bindings")
    assert spec.retention_action == "preserve" and spec.retention_class == "core_evidence"
    restored = tmp_path / "restored.sqlite3"
    with database.connection() as c, closing(sqlite3.connect(restored)) as target:
        c.backup(target)
    recovered = Database(restored)
    assert (
        RelationshipStore(recovered).get(METADATA["relationship_id"]).identity_origin
        == "legacy_observed"
    )
    with recovered.connection() as c:
        recovered_version = int(
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        )
    assert recovered_version == CURRENT_SCHEMA_VERSION
    assert recovered_version >= 107


@pytest.mark.parametrize("reader", ["thought_agenda", "metacognition"])
def test_direct_cognitive_readers_detect_identity_corruption(db: Database, reader: str) -> None:
    from noyra.cognition.metacognition import MetacognitiveControl
    from noyra.cognition.thought import IntrinsicThought

    row = new_relationship(db)
    settings = CognitionSettings()
    gateway = ModelGateway(
        FakeProvider([]),
        ModelLedger(db),
        model="fixture",
        limits=BudgetLimits(10, 100000, 100000, 100000),
    )
    control = MetacognitiveControl(db, SUBJECT, settings)
    thought = IntrinsicThought(db, SUBJECT, gateway, settings)
    with db.transaction() as c:
        c.execute(
            "UPDATE relationships SET affinity=.8,conflict=.1 WHERE relationship_id=?",
            (row.relationship_id,),
        )
        c.execute("DROP TRIGGER prevent_relationship_identity_update")
        c.execute(
            "UPDATE relationships SET entity_key='bob' WHERE relationship_id=?",
            (row.relationship_id,),
        )
    with pytest.raises(IntegrityError):
        if reader == "thought_agenda":
            thought._agenda_candidates()
        else:
            control._candidates()


def test_private_explanation_and_queue_isolation_do_not_expose_private_evidence(
    tmp_path: Path,
) -> None:
    from noyra.cognition.interaction import InteractionCognition
    from noyra.core import SubjectKernel
    from noyra.interaction import InteractionStore, PublicProjection

    path = legacy_copy(tmp_path)
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE relationships SET entity_key='changed-before-upgrade'")
        c.commit()
    kernel = SubjectKernel(path, SUBJECT, content_hash(SUBJECT))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    try:
        messages = InteractionStore(kernel.database)
        messages.receive(
            SUBJECT, "webhook", "changed-before-upgrade", "blocked synthetic invitation"
        )
        good = messages.receive(SUBJECT, "webhook", "unrelated-peer", "unrelated invitation")
        gateway = ModelGateway(
            FakeProvider([]),
            ModelLedger(kernel.database),
            model="fixture",
            limits=BudgetLimits(10, 100000, 100000, 100000),
        )
        cognition = InteractionCognition(kernel.database, SUBJECT, gateway, CognitionSettings())
        selected = cognition._due_interaction()
        assert selected is not None
        assert selected.counterparty != "changed-before-upgrade"
        summary = PublicProjection(kernel.database).private_state(SUBJECT)["relationship_summary"]
        assert summary["legacy_observed_count"] == 1
        assert summary["identity_review_required_count"] == 1
        assert "blocked synthetic invitation" not in json.dumps(summary)
        public = PublicProjection(kernel.database).state(SUBJECT)
        assert "relationship_summary" not in public
        assert good.counterparty == "unrelated-peer"
    finally:
        kernel.close()


def test_export_does_not_include_another_subjects_identity_binding(db: Database) -> None:
    first = new_relationship(db)
    source = EventStore(db).append(OTHER, "observation", "fixture", {})
    other = RelationshipStore(db).ensure(
        OTHER, "human", "alice", "same nickname", source_event_ids=(source.event_id,)
    )
    artifact = RuntimeLogExporter(db).export(SUBJECT, actor="fixture")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        file = next(
            name
            for name in archive.namelist()
            if name.endswith("relationship_identity_bindings.jsonl")
        )
        rows = [json.loads(line) for line in archive.read(file).decode().splitlines()]
    assert {row["relationship_id"] for row in rows} == {first.relationship_id}
    assert other.relationship_id not in {row["relationship_id"] for row in rows}


@pytest.mark.parametrize("table", ["relationships", "relationship_identity_bindings"])
def test_insert_or_replace_cannot_bypass_identity_immutability(db: Database, table: str) -> None:
    row = new_relationship(db)
    with db.connection() as c:
        original = dict(c.execute(f"SELECT * FROM {table}").fetchone())
    values = dict(original)
    values["entity_key"] = "bob"
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as c:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        c.execute(
            f"INSERT OR REPLACE INTO {table}({columns}) VALUES({placeholders})",
            tuple(values.values()),
        )
    assert RelationshipStore(db).get(row.relationship_id).entity_key == "alice"


def test_quarantined_legacy_exception_cannot_hide_new_or_changed_social_history(
    tmp_path: Path,
) -> None:
    path = legacy_copy(tmp_path)
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE relationships SET entity_key='changed-before-upgrade'")
        c.commit()
    database = Database(path)
    with database.connection() as c:
        original = dict(c.execute("SELECT * FROM relationship_social_runs").fetchone())
        InteractionIntegrity._verify_social_run(c, SUBJECT, original)
    for change in ("social_id", "topic"):
        run = dict(original)
        run[change] = "new-unreviewed-value"
        run["state_hash"] = RelationshipSocialCognition._state_hash(
            run["relationship_id"],
            run["model_call_id"],
            run["interaction_id"],
            run["disposition"],
            run["channel"],
            run["counterparty"],
            run["topic"],
            run["rationale"],
            run["proposal_hash"],
            tuple(json.loads(run["evidence_event_ids_json"])),
            run["created_at"],
        )
        with (
            database.connection() as c,
            pytest.raises(IntegrityError, match="quarantined legacy baseline"),
        ):
            InteractionIntegrity._verify_social_run(c, SUBJECT, run)


def test_actual_migration_process_crash_recovers_without_partial_identity_state(
    tmp_path: Path,
) -> None:
    path = legacy_copy(tmp_path)
    with open_readonly(path) as c:
        before = snapshot(c)
    program = """import os,sys
sys.path.insert(0, sys.argv[2])
from unittest.mock import patch
from noyra.core import Database
from noyra.core.relationship_identity import backfill_legacy_bindings
import noyra.core.database as module
def crash(connection):
    backfill_legacy_bindings(connection)
    os._exit(73)
with patch.object(module,'backfill_legacy_bindings',crash):
    Database(sys.argv[1])
"""
    result = subprocess.run(
        # pytest's in-process pythonpath is not inherited by a child. Pin this
        # crash/recovery probe to the current checkout, not an old installation.
        [
            sys.executable,
            "-c",
            program,
            str(path),
            str(Path(__file__).resolve().parents[1] / "src"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    # A read/write SQLite opener performs native hot-journal recovery first.
    with closing(sqlite3.connect(path)) as c:
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "106"
        )
        assert (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='relationship_identity_bindings'"
            ).fetchone()
            is None
        )
    database = Database(path)
    with open_readonly(path) as c:
        assert snapshot(c) == before
    assert (
        RelationshipStore(database).get(METADATA["relationship_id"]).identity_origin
        == "legacy_observed"
    )


def test_schema_107_refuses_an_older_runtime_before_mutation(db: Database) -> None:
    import noyra.core.database as module

    with (
        patch.object(module, "CURRENT_SCHEMA_VERSION", 106),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(db.path)
    db.assert_schema_contract()


def test_privacy_review_refuses_missing_current_identity_storage(db: Database) -> None:
    from noyra.interaction.erasure import ConversationErasure

    with db.transaction() as c:
        c.execute("DROP TABLE relationship_identity_bindings")
    with (
        db.connection() as c,
        pytest.raises(IntegrityError, match="identity storage is unavailable"),
    ):
        ConversationErasure._check_derivatives(c, SUBJECT, set(), [])
