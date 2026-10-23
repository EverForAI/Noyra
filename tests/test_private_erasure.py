from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core import database as database_module
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.private_sources import private_input_scope
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationConflict, ConversationStore, _row
from noyra.interaction.erasure import (
    ERASED,
    ERASED_MODEL,
    ErasureReviewRequired,
    _read_marker,
    replay_privacy_deletions,
)
from noyra.interaction.erasure import (
    ConversationErasure as RecoveryErasure,
)
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.types import InteractionDecision
from noyra.mind import MemoryStore, MindEngine
from noyra.mind.errors import CausalValidationError
from noyra.model.errors import ModelCallStateError
from noyra.model.ledger import ModelLedger
from noyra.model.types import BudgetLimits, ModelUsage


class ConversationErasure(RecoveryErasure):
    """Fixture for intents accepted by the historical prototype, never a product switch."""

    def request(
        self, subject_id: str, conversation_id: str, *, expected_revision: int, confirmed: bool
    ) -> dict[str, Any]:
        if confirmed is not True or type(expected_revision) is not int:
            raise ValueError("explicit deletion confirmation is required")
        with self.database.read_transaction() as connection:
            state = _row(connection, subject_id, conversation_id)
            if self.get(subject_id, conversation_id) is None:
                if state["revision"] != expected_revision:
                    raise ConversationConflict("conversation revision changed")
                self._review(connection, subject_id, conversation_id)
        self._write_marker(subject_id, conversation_id, new_id("erase"))
        return self._resume_recorded_intent(
            subject_id, conversation_id, expected_revision=expected_revision
        )


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


def _private_message(db: Database) -> tuple[dict[str, Any], str, str]:
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
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_embedding_inputs(memory_id, content_hash) VALUES (?, ?)",
            (memory.memory_id, content_hash("private second memory")),
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
        assert not connection.execute("SELECT 1 FROM memory_embedding_inputs").fetchone()
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
    with pytest.raises(CausalValidationError, match="withdrawn"):
        memories.revise(
            memory.memory_id,
            content="late private revival",
            salience=0.5,
            confidence=0.7,
            status="active",
            reason="late",
            source_event_ids=(event,),
        )


def test_deleted_events_cannot_form_new_memories_and_verification_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, event = _private_message(db)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    with monkeypatch.context() as fault:

        def fail_verification(*args: object) -> None:
            raise IntegrityError("injected residual")

        fault.setattr(ConversationErasure, "_verify", staticmethod(fail_verification))
        assert erasure.run("owner", window["conversation_id"])["status"] == "failed"
    assert InteractionStore(db).get(message).content == "private original"
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    with pytest.raises(CausalValidationError, match="withdrawn"):
        MemoryStore(db).create(
            "owner",
            "semantic",
            "late private content",
            salience=0.5,
            confidence=0.5,
            source_event_ids=(event,),
        )
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    from noyra.core.events import EventStore
    from noyra.mind.causal import CausalStore

    with pytest.raises(PermissionError, match="withdrawn"):
        EventStore(db).append(
            "owner",
            "late_copy",
            "test",
            {"private": "late content"},
            causal_parent_ids=(event,),
        )
    with pytest.raises(CausalValidationError, match="withdrawn"):
        CausalStore(db).add(
            "owner",
            "event",
            event,
            "copy",
            "event",
            event,
            strength=0.5,
            metadata={"private": "late content"},
        )


def test_late_unknown_reconciliation_preserves_erasure(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    ledger = ModelLedger(db)
    with private_input_scope((message,)):
        call, _ = ledger.prepare_call(
            "owner", "mock", "mock", "interaction", content_hash("input"), "unknown"
        )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_microusd=1,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(attempt.attempt_id, "unknown", usage=None, cost_microusd=None)
    ledger.finish_call(call.call_id, "unknown")
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    reconciled = ledger.reconcile_unknown(
        call.call_id,
        outcome="succeeded",
        actor="operator",
        reason="private reply",
        response={"content": "late private reply"},
    )
    assert reconciled.response == ERASED_MODEL
    assert reconciled.error_code == "private_content_erased"
    with db.connection() as connection:
        payloads = connection.execute(
            "SELECT payload_json FROM audit_records WHERE action='model_unknown_reconciled'"
        ).fetchall()
    assert all("private reply" not in row[0] for row in payloads)


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


@pytest.mark.parametrize("archived", [False, True])
def test_mixed_memory_reconstructs_only_a_version_preceding_private_input(
    tmp_path: Path, archived: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.events import EventStore

    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior:
        prior.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        independent = EventStore(db).append("owner", "independent_fact", "test", {"fact": "safe"})
    memories = MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00")
    memory = memories.create(
        "owner",
        "episodic",
        "independent original",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(independent.event_id,),
    )
    window, _, private_event = _private_message(db)
    current = MemoryStore(db)
    current.revise(
        memory.memory_id,
        content="mixed private content",
        salience=0.8,
        confidence=0.9,
        status="archived" if archived else "active",
        reason="mixed private rationale",
        source_event_ids=(private_event, independent.event_id),
    )
    # Merely switching the declared source after reading private text cannot prove independence.
    current.revise(
        memory.memory_id,
        content="late private copied content",
        salience=0.8,
        confidence=0.9,
        status="archived" if archived else "active",
        reason="late copied rationale",
        source_event_ids=(independent.event_id,),
    )
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    job = erasure.run("owner", window["conversation_id"])
    assert job["status"] == "completed"
    assert job["counts"]["reconstructed_memories"] == 1
    assert job["counts"]["exclusive_memories"] == 0
    original = current.get(memory.memory_id)
    assert original.status == "archived" and original.content == ERASED
    assert all(row["content"] == ERASED for row in current.revisions(memory.memory_id))
    with db.connection() as connection:
        replacements = connection.execute(
            "SELECT * FROM memories WHERE memory_id!=?",
            (memory.memory_id,),
        ).fetchall()
        assert len(replacements) == 1
        replacement = replacements[0]
        assert replacement["content"] == "independent original"
        assert replacement["status"] == ("archived" if archived else "active")
        assert current.source_event_ids(replacement["memory_id"]) == (independent.event_id,)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 2
    assert MindEngine(db).verify_integrity("owner")["memories"] == 2


def test_mixed_memory_cannot_use_later_revision_hidden_by_timezone_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.events import EventStore

    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior:
        prior.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        independent = EventStore(db).append("owner", "independent", "test", {"fact": "safe"})
    with monkeypatch.context() as private:
        private.setattr("noyra.core.events.utc_now", lambda: "2000-01-01T00:00:00.000+00:00")
        window, _, event = _private_message(db)
    memory = MemoryStore(db, clock=lambda: "1999-12-31T23:30:00.000-01:00").create(
        "owner",
        "episodic",
        "private copied text",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(independent.event_id,),
    )
    MemoryStore(db).revise(
        memory.memory_id,
        content="mixed private",
        salience=0.5,
        confidence=0.7,
        status="active",
        reason="private",
        source_event_ids=(independent.event_id, event),
    )
    with pytest.raises(ErasureReviewRequired, match="multi_source_memory_review_required"):
        ConversationErasure(db).request(
            "owner", window["conversation_id"], expected_revision=1, confirmed=True
        )


def test_cross_window_model_derivatives_require_review(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    other = ConversationStore(db).create("owner", "independent")
    second = InteractionStore(db).receive(
        "owner", "web", "web-user", "other message", conversation_id=other["conversation_id"]
    )
    with private_input_scope((message, second.interaction_id)):
        ledger = ModelLedger(db)
        call, _ = ledger.prepare_call(
            "owner", "mock", "mock", "interaction", content_hash("input"), "mixed-model"
        )
    ledger.finish_call(call.call_id, "failed", error_code="test")
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="cross_window_derivative_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert InteractionStore(db).get(message).content == "private original"


def test_causal_event_copy_requires_review(tmp_path: Path) -> None:
    from noyra.core.events import EventStore

    db = Database(tmp_path / "noyra.sqlite3")
    window, _, event = _private_message(db)
    EventStore(db).append(
        "owner",
        "private_derived",
        "test",
        {"private": "copied text"},
        causal_parent_ids=(event,),
    )
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="private_event_derivative_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()


@pytest.mark.parametrize("interruption", ["marker", "pending", "running", "failed"])
def test_recovery_resumes_only_recorded_subject_intents(tmp_path: Path, interruption: str) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    erasure = ConversationErasure(db)
    if interruption == "marker":
        erasure._write_marker("owner", window["conversation_id"], new_id("erase"))
    else:
        job = erasure.request(
            "owner", window["conversation_id"], expected_revision=1, confirmed=True
        )
        if interruption != "pending":
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE private_erasure_jobs SET status=? WHERE job_id=?",
                    (interruption, job["job_id"]),
                )
    assert erasure.resume("other") == []
    assert InteractionStore(db).get(message).content == "private original"
    recovered = ConversationErasure(Database(db.path)).resume("owner")
    assert len(recovered) == 1 and recovered[0]["status"] == "completed"
    assert InteractionStore(db).get(message).content == ERASED
    assert erasure.resume("owner") == []


def test_recovery_recreates_lost_marker_from_accepted_database_intent(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    erasure = ConversationErasure(db)
    job = erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    next(erasure.root.iterdir()).unlink()
    assert erasure.resume("owner")[0]["status"] == "completed"
    assert _read_marker(next(erasure.root.iterdir()))["job_id"] == job["job_id"]


def test_recovery_skips_orphan_markers_without_starving_real_jobs(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    erasure = ConversationErasure(db)
    for index in range(16):
        erasure._write_marker("owner", f"missing-{index}", new_id("erase"))
    job = erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)

    recovered = erasure.resume("owner", limit=1)

    assert len(recovered) == 1
    assert recovered[0]["job_id"] == job["job_id"]
    assert recovered[0]["status"] == "completed"


def test_recovery_does_not_admit_an_unreviewed_orphan_intent(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    erasure = ConversationErasure(db)
    erasure._write_marker("owner", window["conversation_id"], new_id("erase"))
    ModelLedger(db).prepare_call(
        "owner", "mock", "mock", "legacy", content_hash("unknown"), "untracked"
    )
    with pytest.raises(ErasureReviewRequired, match="legacy_private_provenance_review_required"):
        erasure.resume("owner")
    assert erasure.get("owner", window["conversation_id"]) is None
    assert InteractionStore(db).get(message).content == "private original"


def test_managed_export_retirement_is_retryable_and_blocks_late_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.export_jobs import ExportCancelledError, ExportControl

    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    root = tmp_path / "exports" / "jobs"
    root.mkdir(parents=True)
    job_id = new_id("export-job")
    target = root / f"{job_id}.zip"
    target.write_bytes(b"private exported content")
    partial = root / f".{job_id}.zip.partial.tmp"
    partial.write_bytes(b"private partial content")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO export_jobs(job_id,subject_id,export_kind,status,artifact_path,"
            "created_at) "
            "VALUES(?,?,'runtime','completed',?,?)",
            (job_id, "owner", str(target), utc_now()),
        )
    stale = ExportControl(db, job_id, "owner", target, max_artifact_bytes=1000)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    unlink = Path.unlink
    with monkeypatch.context() as fault:

        def fail_unlink(path: Path, missing_ok: bool = False) -> None:
            if path == target:
                raise OSError("simulated busy artifact")
            unlink(path, missing_ok=missing_ok)

        fault.setattr(Path, "unlink", fail_unlink)
        assert erasure.run("owner", window["conversation_id"])["status"] == "failed"
    assert InteractionStore(db).get(message).content == "private original"
    with db.connection() as connection:
        assert connection.execute("SELECT error_code FROM export_jobs").fetchone()[0] == (
            "privacy_erasure_retiring"
        )
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    assert not target.exists() and not partial.exists()
    with pytest.raises(ExportCancelledError, match="changed"):
        stale.checkpoint()
    with db.connection() as connection:
        row = connection.execute("SELECT artifact_path,error_code FROM export_jobs").fetchone()
        assert row[0] is None and row[1] == "privacy_erasure_retired"


def test_inflight_exports_are_not_accepted_as_clearable(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO export_jobs(job_id,subject_id,export_kind,status,created_at) "
            "VALUES(?,'owner','runtime','running',?)",
            (new_id("export-job"), utc_now()),
        )
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="private_exports_in_flight"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert InteractionStore(db).get(message).content == "private original"


def test_restore_retires_managed_export_in_restored_root(tmp_path: Path) -> None:
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    root = current / "exports" / "jobs"
    root.mkdir(parents=True)
    job_id = new_id("export-job")
    target = root / f"{job_id}.zip"
    target.write_bytes(b"private snapshot")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO export_jobs(job_id,subject_id,export_kind,status,artifact_path,"
            "created_at) VALUES(?,'owner','runtime','completed',?,?)",
            (job_id, str(target), utc_now()),
        )
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "before.noyra-backup"
    manager.create(backup)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    restored_root = tmp_path / "restored"

    manager.restore(backup, restored_root)

    assert target.exists()
    assert not (restored_root / "exports" / "jobs" / target.name).exists()
    restored = Database(restored_root / "noyra.sqlite3")
    assert InteractionStore(restored).get(message).content == ERASED
    with restored.connection() as connection:
        assert connection.execute("SELECT artifact_path FROM export_jobs").fetchone()[0] is None


def test_service_boot_replays_privacy_before_runtime_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.service import NoyraService, ServiceSettings

    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="recovery-owner",
        genesis_hash=content_hash("owner"),
        host="127.0.0.1",
        port=0,
    )
    service = NoyraService(settings)
    try:
        subject = settings.subject_id
        window = ConversationStore(service.kernel.database).create(subject, "private title")
        message = (
            InteractionStore(service.kernel.database)
            .receive(
                subject,
                "web",
                "web-user",
                "private original",
                conversation_id=window["conversation_id"],
            )
            .interaction_id
        )
        ConversationErasure(service.kernel.database)._write_marker(
            subject, window["conversation_id"], new_id("erase")
        )
        original = service.kernel.recover_after_integrity

        def runtime_recovery() -> object:
            assert InteractionStore(service.kernel.database).get(message).content == ERASED
            return original()

        monkeypatch.setattr(service.kernel, "recover_after_integrity", runtime_recovery)
        service.boot()
        assert service.kernel.lifecycle.current().state == "active"
    finally:
        service.close()


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


def test_restore_reconstructs_independent_memory_and_erases_mixed_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.events import EventStore

    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior:
        prior.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        independent = EventStore(db).append("owner", "independent", "test", {"fact": "safe"})
    memory = MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00").create(
        "owner",
        "episodic",
        "independent fact",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(independent.event_id,),
    )
    window, message, event = _private_message(db)
    MemoryStore(db).revise(
        memory.memory_id,
        content="private mixed fact",
        salience=0.8,
        confidence=0.9,
        status="active",
        reason="private reason",
        source_event_ids=(independent.event_id, event),
    )
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "before.noyra-backup"
    manager.create(backup)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"

    target = tmp_path / "restored"
    manager.restore(backup, target)

    restored = Database(target / "noyra.sqlite3")
    assert InteractionStore(restored).get(message).content == ERASED
    assert MemoryStore(restored).get(memory.memory_id).content == ERASED
    assert all(
        row["content"] == ERASED for row in MemoryStore(restored).revisions(memory.memory_id)
    )
    remaining = MemoryStore(restored).search("owner", query="independent")
    assert len(remaining) == 1 and remaining[0].content == "independent fact"
    assert MindEngine(restored).verify_integrity("owner")["memories"] == 2


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
            == database_module._SCHEMA_DDL_FINGERPRINTS[database_module.CURRENT_SCHEMA_VERSION]
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
    with pytest.raises(
        ModelCallStateError, match="cannot authorize attempt for call in state failed"
    ):
        ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(10, 1000, 1000, 1000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=1,
        )
    assert not ledger.attempts(call.call_id)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"


def test_job_and_receipt_guards_preserve_owner_and_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, _ = _private_message(db)
    IdentityStore(db).ensure("other", content_hash("other"))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
        connection.execute(
            "INSERT INTO private_erasure_jobs VALUES(?,?,?,'pending','fence',NULL,'{}',?,?)",
            (new_id("erase"), "other", window["conversation_id"], utc_now(), utc_now()),
        )
    erasure = ConversationErasure(db)
    job = erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
        connection.execute(
            "INSERT INTO private_erasure_receipts VALUES(?,'memories','fake','old','new')",
            (job["job_id"],),
        )
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    for statement in (
        "UPDATE private_erasure_jobs SET subject_id='other' WHERE job_id=?",
        "UPDATE private_erasure_jobs SET status='running' WHERE job_id=?",
        "DELETE FROM private_erasure_jobs WHERE job_id=?",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
            connection.execute(statement, (job["job_id"],))


def test_schema87_migration_installs_job_guards_without_changing_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 87)
        db = Database(tmp_path / "noyra.sqlite3")
        window, _, _ = _private_message(db)
        erasure = ConversationErasure(db)
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
        assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
        with db.connection() as connection:
            original = [
                tuple(row) for row in connection.execute("SELECT * FROM private_erasure_receipts")
            ]
    migrated = Database(db.path)
    with migrated.connection() as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM private_erasure_receipts")
        ] == original
        assert (
            Database.schema_ddl_fingerprint(connection)
            == database_module._SCHEMA_DDL_FINGERPRINTS[database_module.CURRENT_SCHEMA_VERSION]
        )
    with pytest.raises(sqlite3.IntegrityError), migrated.transaction() as connection:
        connection.execute("UPDATE private_erasure_jobs SET status='running'")
