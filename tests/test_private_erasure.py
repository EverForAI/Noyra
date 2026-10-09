from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.private_sources import private_input_scope
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationConflict, ConversationStore
from noyra.interaction.erasure import (
    ERASED,
    ERASED_MODEL,
    ConversationErasure,
    ErasureReviewRequired,
    _read_marker,
    replay_privacy_deletions,
)
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.types import InteractionDecision
from noyra.mind import MemoryStore, MindEngine
from noyra.model.errors import ModelCallStateError
from noyra.model.ledger import ModelLedger
from noyra.model.types import BudgetLimits, ModelUsage


def test_erasure_preserves_accounting_and_blocks_late_writes(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    windows = ConversationStore(db)
    window = windows.create("owner", "private title")
    messages = InteractionStore(db)
    message = messages.receive(
        "owner", "web", "web-user", "private raw text", conversation_id=window["conversation_id"]
    )
    messages.decide(
        message.interaction_id,
        InteractionDecision(disposition="accepted", rationale="private rationale"),
    )
    ledger = ModelLedger(db)
    with private_input_scope((message.interaction_id,)):
        call, _ = ledger.prepare_call(
            "owner",
            "mock",
            "mock",
            "interaction",
            content_hash("input"),
            "first",
            request={"content": "private raw text"},
        )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=100,
        reserved_output_tokens=100,
        reserved_cost_microusd=100,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(
        attempt.attempt_id, "succeeded", usage=ModelUsage(50, 50), cost_microusd=50
    )
    ledger.finish_call(call.call_id, "succeeded", response={"content": "private reply"})
    erasure = ConversationErasure(db)
    with pytest.raises(ValueError):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=False)
    job = erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert job["status"] == "pending"
    with (
        pytest.raises(PermissionError),
        private_input_scope((message.interaction_id,)),
        db.transaction(),
    ):
        pass
    with pytest.raises(ConversationConflict):
        messages.send(
            "owner", "web", "web-user", "late", related_interaction_id=message.interaction_id
        )
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed"
    assert windows.list("owner")["items"] == []
    assert messages.get(message.interaction_id).content == ERASED
    assert ledger.get_call(call.call_id).response == ERASED_MODEL
    assert ledger.attempts(call.call_id)[0].cost_microusd == 50
    assert InteractionIntegrity(db).verify("owner")["interactions"] == 1
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    with db.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE interactions SET content='revived' WHERE interaction_id=?",
            (message.interaction_id,),
        )
    assert "private raw text" not in next(erasure.root.glob("*.json")).read_text()
    assert Database(db.path)


def test_opaque_legacy_provenance_fails_without_claiming_erasure(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private")
    message = InteractionStore(db).receive(
        "owner", "web", "web-user", "original", conversation_id=window["conversation_id"]
    )
    ModelLedger(db).prepare_call(
        "owner", "mock", "mock", "sleep_reflection:legacy", content_hash("input"), "legacy"
    )
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="legacy_private_provenance_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.get("owner", window["conversation_id"]) is None
    assert not erasure.root.exists()
    assert ConversationStore(db).list("owner")["items"][0]["status"] == "active"
    assert InteractionStore(db).get(message.interaction_id).content == "original"
    assert InteractionIntegrity(db).verify("owner")["interactions"] == 1


def _private_message(db: Database) -> tuple[dict, str, str]:
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    message = InteractionStore(db).receive(
        "owner", "web", "web-user", "private original", conversation_id=window["conversation_id"]
    )
    with db.connection() as connection:
        event = connection.execute(
            "SELECT event_id FROM events WHERE subject_id='owner' "
            "AND event_type='interaction_received' "
            "AND json_extract(payload_json,'$.interaction_id')=?",
            (message.interaction_id,),
        ).fetchone()[0]
    return window, message.interaction_id, event


@pytest.mark.parametrize("archived", [False, True])
def test_exclusive_memory_revisions_and_vector_are_erased(tmp_path: Path, archived: bool) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, event = _private_message(db)
    memories = MemoryStore(db)
    memory = memories.create(
        "owner",
        "episodic",
        "private first memory",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(event,),
    )
    memories.revise(
        memory.memory_id,
        content="private second memory",
        salience=0.6,
        confidence=0.8,
        status="archived" if archived else "active",
        reason="private reason",
        source_event_ids=(event,),
    )
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,'mock',2,?,?,?)",
            (
                memory.memory_id,
                "owner",
                canonical_json([0.2, 0.4]),
                content_hash([0.2, 0.4]),
                utc_now(),
            ),
        )
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    job = erasure.run("owner", window["conversation_id"])
    assert job["status"] == "completed"
    assert job["counts"]["exclusive_memories"] == 1
    current = memories.get(memory.memory_id)
    assert current.status == "archived" and current.content == ERASED
    assert current.current_revision == 3
    assert all(revision["content"] == ERASED for revision in memories.revisions(memory.memory_id))
    assert all(
        revision["reason"] == "privacy erasure" for revision in memories.revisions(memory.memory_id)
    )
    assert memories.search("owner", query="private") == []
    assert memories.recall("owner", "private", context_type="test", context_id="test") == []
    assert MindEngine(db).verify_integrity("owner")["memories"] == 1
    with db.connection() as connection:
        assert not connection.execute("SELECT 1 FROM memory_embeddings").fetchone()
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
        connection.execute(
            "UPDATE memories SET status='active' WHERE memory_id=?", (memory.memory_id,)
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,'mock',2,?,?,?)",
            (
                memory.memory_id,
                "owner",
                canonical_json([0.2, 0.4]),
                content_hash([0.2, 0.4]),
                utc_now(),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        memories.revise(
            memory.memory_id,
            content="late private revival",
            salience=0.5,
            confidence=0.7,
            status="active",
            reason="late",
            source_event_ids=(event,),
        )


def test_mixed_source_memory_requires_review_before_accepting_intent(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, first_event = _private_message(db)
    other = ConversationStore(db).create("owner", "independent")
    second = InteractionStore(db).receive(
        "owner", "web", "web-user", "independent fact", conversation_id=other["conversation_id"]
    )
    with db.connection() as connection:
        second_event = connection.execute(
            "SELECT event_id FROM events WHERE event_type='interaction_received' AND "
            "json_extract(payload_json,'$.interaction_id')=?",
            (second.interaction_id,),
        ).fetchone()[0]
    memory = MemoryStore(db).create(
        "owner",
        "episodic",
        "mixed facts",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(first_event, second_event),
    )
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="multi_source_memory_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).get(memory.memory_id).content == "mixed facts"
    assert len(ConversationStore(db).list("owner")["items"]) == 2


def test_erasure_conflict_and_ownership_leave_no_marker(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    erasure = ConversationErasure(db)
    with pytest.raises(ConversationConflict):
        erasure.request("owner", window["conversation_id"], expected_revision=0, confirmed=True)
    with pytest.raises(NotFoundError):
        erasure.request("other", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()


def test_restore_replays_intent_written_before_database_commit(tmp_path: Path) -> None:
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "before.noyra-backup"
    manager.create(backup)
    erasure = ConversationErasure(db)
    erasure._write_marker("owner", window["conversation_id"], new_id("erase"))
    assert erasure.get("owner", window["conversation_id"]) is None
    target = tmp_path / "restored"
    manager.restore(backup, target)
    restored = Database(target / "noyra.sqlite3")
    assert ConversationStore(restored).list("owner")["items"] == []
    assert InteractionStore(restored).get(message).content == ERASED
    assert InteractionStore(db).get(message).content == "private original"
    assert next((target / "privacy-deletions").iterdir()).is_file()
    assert InteractionIntegrity(restored).verify("owner")["interactions"] == 1


def test_restore_rejects_unsupported_replay_without_publishing(tmp_path: Path) -> None:
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    ModelLedger(db).prepare_call(
        "owner", "mock", "mock", "reflection", content_hash("input"), "old"
    )
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "before.noyra-backup"
    manager.create(backup)
    ConversationErasure(db)._write_marker("owner", window["conversation_id"], new_id("erase"))
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(ErasureReviewRequired):
        manager.restore(backup, target)
    assert target.is_dir() and list(target.iterdir()) == []
    assert ConversationStore(db).list("owner")["items"]
    assert not list(tmp_path.glob(".noyra-restore-*"))


@pytest.mark.parametrize("invalid", ["empty", "malformed", "wrong_filename", "large", "hardlink"])
def test_invalid_markers_block_restore(tmp_path: Path, invalid: str) -> None:
    root = tmp_path / "privacy-deletions"
    root.mkdir()
    marker = dict(
        format="noyra-private-deletion/v1",
        subject_id="owner",
        conversation_id="window",
        job_id=new_id("erase"),
    )
    path = root / (content_hash(["owner", "window"]) + ".json")
    path.write_text(canonical_json(marker), encoding="utf-8")
    if invalid == "empty":
        path.write_bytes(b"")
    elif invalid == "malformed":
        path.write_text("[]", encoding="utf-8")
    elif invalid == "wrong_filename":
        path = path.rename(root / "wrong.json")
    elif invalid == "large":
        path.write_bytes(b" " * 4097)
    elif invalid == "hardlink":
        os.link(path, root / "other.json")
    with pytest.raises(IntegrityError):
        _read_marker(path)
    with pytest.raises(IntegrityError):
        replay_privacy_deletions(tmp_path, tmp_path / "restored")
    assert not (tmp_path / "restored" / "noyra.sqlite3").exists()


def test_review_row_limit_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    monkeypatch.setattr("noyra.interaction.erasure.MAX_ERASURE_ROWS", 0)
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="private_erasure_scope_exceeds_limit"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()


def test_schema86_migration_preserves_content_and_installs_erasure_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 86)
        db = Database(tmp_path / "noyra.sqlite3")
        window, message, _ = _private_message(db)
    migrated = Database(db.path)
    assert InteractionStore(migrated).get(message).content == "private original"
    with migrated.connection() as connection:
        assert (
            Database.schema_ddl_fingerprint(connection)
            == database_module._SCHEMA_DDL_FINGERPRINTS[87]
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    erasure = ConversationErasure(migrated)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    assert Database(migrated.path)


def test_backup_without_deletion_marker_preserves_old_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current"
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 86)
        db = Database(current / "noyra.sqlite3")
        _private_message(db)
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "historical.noyra-backup"
    manager.create(backup)
    target = manager.restore(backup, tmp_path / "restored")
    with sqlite3.connect(target / "noyra.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "86"
        )


def test_inflight_model_call_is_rejected_without_closing_window(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    ledger = ModelLedger(db)
    with private_input_scope((message,)):
        call, _ = ledger.prepare_call(
            "owner", "mock", "mock", "interaction", content_hash("input"), "new"
        )
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="private_calls_in_flight"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert ConversationStore(db).list("owner")["items"][0]["status"] == "active"
    ledger.finish_call(call.call_id, "failed")
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    with pytest.raises(ModelCallStateError, match="private source has been withdrawn"):
        ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(10, 1000, 1000, 1000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
