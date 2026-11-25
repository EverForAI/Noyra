from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, EventStore, IdentityStore
from noyra.core import database as database_module
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.export_jobs import ExportCancelledError
from noyra.core.private_erasure_schema import ERASED_REFLECTION_HASH, ERASED_REFLECTION_PLAN
from noyra.core.private_sources import private_input_scope
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationConflict, ConversationStore, _row
from noyra.interaction.diary import PublicDiaryStore
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
from noyra.interaction.errors import InteractionStateConflictError
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.types import InteractionDecision
from noyra.mind import (
    EntityStore,
    MemoryBlockStore,
    MemoryConsolidator,
    MemoryIntegrationSupervisor,
    MemoryStore,
    MindEngine,
)
from noyra.mind.errors import CausalValidationError
from noyra.model.errors import ModelCallStateError
from noyra.model.ledger import ModelLedger
from noyra.model.types import BudgetLimits, ModelUsage
from noyra.sleep import SleepEngine, SleepIntegrity, SleepMemory, SleepReflectionPlan


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


@pytest.mark.parametrize("derivative", ["memory_block", "entity_graph"])
def test_memory_derivative_domains_use_verified_erasure_boundaries(
    tmp_path: Path, derivative: str
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message_id, event_id = _private_message(db)

    if derivative == "memory_block":
        blocks = MemoryBlockStore(db)
        block = blocks.create(
            "owner",
            "working_context",
            "private conversation context",
            "A summary that may contain conversation-derived details.",
            reason="captured context",
            source_memory_ids=(),
            source_event_ids=(event_id,),
        )
        block_lineage = blocks.lineage_history(block.block_id)[0]
        assert block_lineage.event_lineage_complete is True
        assert tuple(event.event_id for event in block_lineage.event_sources) == (event_id,)

        erasure = ConversationErasure(db)
        assessment = erasure.assess("owner", window["conversation_id"])
        assert assessment["status"] == "full_contract_pending"
        assert assessment["deletion_available"] is False

        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
        result = erasure.run("owner", window["conversation_id"])
        assert result["status"] == "completed"
        assert result["counts"]["memory_block_revisions"] == 1
        assert result["counts"]["memory_blocks"] == 1
        assert blocks.get(block.block_id).content == ERASED
        assert blocks.get(block.block_id).status == "archived"
        assert blocks.verify_integrity("owner")["memory_block_revisions"] == 1
        with db.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM private_erasure_receipts "
                "WHERE record_table='memory_block_revisions'"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM private_erasure_receipts "
                "WHERE record_table='memory_blocks'"
            ).fetchone()[0] == 1
        assert InteractionStore(db).get(message_id).content == ERASED
        return

    entities = EntityStore(db)
    entity = entities.upsert("owner", "person", "private-person", "Private conversation person")
    link = entities.link_evidence(
        "owner",
        entity.entity_id,
        "event",
        event_id,
        role="mentioned in private conversation",
        confidence=0.9,
    )

    erasure = ConversationErasure(db)
    assessment = erasure.assess("owner", window["conversation_id"])
    assert assessment["status"] == "review_required"
    assert assessment["reason_code"] == "private_memory_derivative_review_required"
    assert assessment["deletion_available"] is False
    assert assessment["counts"] is None

    with pytest.raises(
        ErasureReviewRequired, match="private_memory_derivative_review_required"
    ):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)

    assert not erasure.root.exists()
    assert InteractionStore(db).get(message_id).content == "private original"
    assert ConversationStore(db).list("owner")["items"][0]["status"] == "active"
    assert entities.evidence_links("owner", entity.entity_id) == [link]
    assert entities.verify_integrity("owner")["links"] == 1


def test_memory_block_with_mixed_event_sources_remains_fail_closed(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    independent_event_id = EventStore(db).append("owner", "experience", "test", {}).event_id
    MemoryBlockStore(db).create(
        "owner",
        "working_context",
        "mixed context",
        "Mixed private and independent evidence.",
        reason="mixed capture",
        source_memory_ids=(),
        source_event_ids=(private_event_id, independent_event_id),
    )

    erasure = ConversationErasure(db)
    with pytest.raises(
        ErasureReviewRequired, match="private_memory_derivative_review_required"
    ):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)


def test_memory_block_with_affected_memory_source_remains_fail_closed(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    memory = MemoryStore(db).create(
        "owner",
        "semantic",
        "Private memory source.",
        salience=0.8,
        confidence=0.9,
        source_event_ids=(private_event_id,),
    )
    MemoryBlockStore(db).create(
        "owner",
        "working_context",
        "memory-derived context",
        "Derived from a private memory.",
        reason="derived capture",
        source_memory_ids=(memory.memory_id,),
        source_event_ids=(),
    )

    erasure = ConversationErasure(db)
    with pytest.raises(
        ErasureReviewRequired, match="private_memory_derivative_review_required"
    ):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)


def test_legacy_memory_block_without_event_coverage_remains_fail_closed(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    blocks = MemoryBlockStore(db)
    block = blocks.create(
        "owner",
        "working_context",
        "legacy context",
        "Legacy context without direct event coverage.",
        reason="legacy capture",
        source_memory_ids=(),
        source_event_ids=(private_event_id,),
    )
    with db.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_memory_block_revision_event_source_delete")
        connection.execute("DROP TRIGGER prevent_memory_block_revision_event_coverage_delete")
        connection.execute("DROP TRIGGER prevent_memory_block_revision_lineage_update")
        connection.execute(
            "DELETE FROM memory_block_revision_event_sources WHERE revision_id IN "
            "(SELECT revision_id FROM memory_block_revisions WHERE block_id=?)",
            (block.block_id,),
        )
        connection.execute(
            "DELETE FROM memory_block_revision_event_coverage WHERE revision_id IN "
            "(SELECT revision_id FROM memory_block_revisions WHERE block_id=?)",
            (block.block_id,),
        )
        connection.execute(
            "UPDATE memory_block_revision_lineage SET lineage_hash=? WHERE revision_id IN "
            "(SELECT revision_id FROM memory_block_revisions WHERE block_id=?)",
            (MemoryBlockStore._lineage_hash("declared_no_memory_sources", ()), block.block_id),
        )

    erasure = ConversationErasure(db)
    with pytest.raises(
        ErasureReviewRequired, match="private_memory_derivative_review_required"
    ):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)



def test_unrelated_memory_block_survives_private_event_erasure(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    independent_event_id = EventStore(db).append("owner", "experience", "test", {}).event_id
    blocks = MemoryBlockStore(db)
    block = blocks.create(
        "owner",
        "working_context",
        "independent context",
        "Independent context remains readable.",
        reason="independent capture",
        source_memory_ids=(),
        source_event_ids=(independent_event_id,),
    )

    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    assert blocks.get(block.block_id).content == "Independent context remains readable."
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM private_erasure_receipts "
            "WHERE record_table IN ('memory_blocks','memory_block_revisions')"
        ).fetchone()[0] == 0
    assert private_event_id


def test_historical_memory_block_revision_is_redacted_without_erasing_current_revision(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    independent_event_id = EventStore(db).append("owner", "experience", "test", {}).event_id
    blocks = MemoryBlockStore(db)
    block = blocks.create(
        "owner",
        "working_context",
        "versioned context",
        "Private historical context.",
        reason="private capture",
        source_memory_ids=(),
        source_event_ids=(private_event_id,),
    )
    blocks.revise(
        block.block_id,
        "Independent current context.",
        reason="independent refresh",
        expected_version=1,
        source_memory_ids=(),
        source_event_ids=(independent_event_id,),
    )

    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed"
    assert result["counts"]["memory_block_revisions"] == 1
    assert result["counts"]["memory_blocks"] == 0
    assert blocks.get(block.block_id).content == "Independent current context."
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT version,content,reason FROM memory_block_revisions "
            "WHERE block_id=? ORDER BY version",
            (block.block_id,),
        ).fetchall()
    assert [(row["version"], row["content"], row["reason"]) for row in rows] == [
        (1, ERASED, "privacy erasure"),
        (2, "Independent current context.", "independent refresh"),
    ]


def test_backup_restore_replays_direct_event_memory_block_erasure(tmp_path: Path) -> None:
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    block = MemoryBlockStore(db).create(
        "owner",
        "working_context",
        "backup context",
        "Backup must not restore erased block content.",
        reason="private capture",
        source_memory_ids=(),
        source_event_ids=(private_event_id,),
    )
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "before-erasure.noyra-backup"
    manager.create(backup)

    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"

    target = tmp_path / "restored"
    manager.restore(backup, target)
    restored = Database(target / "noyra.sqlite3")
    assert MemoryBlockStore(restored).get(block.block_id).content == ERASED
    assert MemoryBlockStore(restored).get(block.block_id).status == "archived"



def test_erased_memory_block_cannot_be_restored_by_direct_sql(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, _, private_event_id = _private_message(db)
    blocks = MemoryBlockStore(db)
    block = blocks.create(
        "owner",
        "working_context",
        "restoration guard",
        "Private block content.",
        reason="private capture",
        source_memory_ids=(),
        source_event_ids=(private_event_id,),
    )
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    with db.connection() as connection:
        revision_id = connection.execute(
            "SELECT revision_id FROM memory_block_revisions WHERE block_id=?",
            (block.block_id,),
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError, match="memory block revisions require explicit content erasure"
        ):
            connection.execute(
                "UPDATE memory_block_revisions SET content=?,content_hash=? WHERE revision_id=?",
                ("restored", content_hash("restored"), revision_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="erased memory block content cannot be restored"
        ):
            connection.execute(
                "UPDATE memory_blocks SET content=?,content_hash=?,status='active' "
                "WHERE block_id=?",
                ("restored", content_hash("restored"), block.block_id),
            )



def _sleep_reflection(
    db: Database,
    inputs: tuple[str, ...],
    text: str,
    *,
    bound: bool = True,
    complete: bool = True,
    match_response: bool = True,
    memories: tuple[SleepMemory, ...] = (),
) -> tuple[str, str]:
    engine = SleepEngine(db, "owner")
    engine.lifecycle.ensure_initial("active")
    sleep_id = engine.start("subject_choice", "fixture sleep").sleep_id
    engine.begin_reflection(sleep_id)
    plan = SleepReflectionPlan(
        summary=f"{text} summary",
        facts=(f"{text} fact",),
        contradictions=(f"{text} contradiction",),
        prediction_errors=(f"{text} prediction error",),
        unresolved_questions=(f"{text} question",),
        public_diary_candidate=f"{text} diary candidate",
        memories=memories,
    )
    ledger = ModelLedger(db)
    with private_input_scope(inputs):
        call, _ = ledger.prepare_call(
            "owner",
            "mock",
            "mock",
            f"sleep_reflection:{sleep_id}",
            content_hash(text),
            sleep_id,
            request={"content": text},
        )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 1000, 1000, 1000),
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=10,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(attempt.attempt_id, "succeeded", usage=ModelUsage(5, 5), cost_microusd=5)
    response_plan = plan if match_response else SleepReflectionPlan(summary="another response")
    ledger.finish_call(
        call.call_id, "succeeded", response={"content": response_plan.model_dump_json()}
    )
    if bound:
        plan._model_call_id = call.call_id
    engine.commit_reflection(sleep_id, plan)
    if complete:
        engine.enter_deep_sleep(sleep_id)
        engine.wake(sleep_id, "fixture wake", force=True)
        engine.complete_wake(sleep_id)
    return sleep_id, call.call_id


def _reflection_row(db: Database, sleep_id: str) -> dict[str, Any]:
    with db.read_transaction() as connection:
        return dict(
            connection.execute(
                "SELECT * FROM sleep_reflections WHERE sleep_id=?", (sleep_id,)
            ).fetchone()
        )


def test_sleep_reflection_erasure_replays_encrypted_backups_across_schema120(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "current"
    secret = "fixture-reflection-cleartext-487230"
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 119)
        db = Database(root / "noyra.sqlite3")
        window, message, _ = _private_message(db)
        other_window = ConversationStore(db).create("owner", "independent window")
        other_message = InteractionStore(db).receive(
            "owner",
            "web",
            "other-web-user",
            "independent message",
            conversation_id=other_window["conversation_id"],
        )
        sleep_id, call_id = _sleep_reflection(db, (message,), secret)
        other_sleep, other_call = _sleep_reflection(
            db, (other_message.interaction_id,), "independent reflection"
        )
    original = _reflection_row(db, sleep_id)
    independent = _reflection_row(db, other_sleep)
    original_other_call = ModelLedger(db).get_call(other_call)
    run = SleepEngine(db, "owner").get(sleep_id)
    chain = EventStore(db).verify_chain("owner")
    integrity = SleepIntegrity(db).verify("owner")
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(root, keyring)
    before = tmp_path / "before-erasure.noyra-backup"
    manager.create(before)
    assert secret.encode() in db.path.read_bytes()

    db = Database(db.path, strict_schema_contract=True)
    assert _reflection_row(db, sleep_id) == original
    assert not list(root.glob("noyra.sqlite3.pre-migration-v*.bak"))
    erasure = ConversationErasure(db)
    with pytest.raises(ErasureReviewRequired, match="private_deletion_disabled"):
        RecoveryErasure(db).request(
            "owner", window["conversation_id"], expected_revision=1, confirmed=True
        )
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    assert result["counts"]["sleep_reflections"] == 1
    assert erasure.run("owner", window["conversation_id"]) == result
    after = tmp_path / "after-erasure.noyra-backup"
    manager.create(after)

    restored = []
    for index, backup in enumerate((before, after)):
        destination = tmp_path / f"restored-{index}"
        manager.restore(backup, destination)
        copy = Database(
            destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
        )
        restored.append(copy)
        assert secret.encode() not in copy.path.read_bytes()
        assert not any(
            secret.encode() in path.read_bytes() for path in destination.glob("*.sqlite3-*")
        )
    for copy in [db, *restored]:
        assert _reflection_row(copy, sleep_id) == {
            **original,
            "summary": ERASED,
            "facts_json": "[]",
            "contradictions_json": "[]",
            "prediction_errors_json": "[]",
            "unresolved_questions_json": "[]",
            "public_diary_candidate": None,
            "plan_json": ERASED_REFLECTION_PLAN,
            "plan_hash": ERASED_REFLECTION_HASH,
        }
        assert _reflection_row(copy, other_sleep) == independent
        assert ModelLedger(copy).get_call(other_call) == original_other_call
        assert InteractionStore(copy).get(other_message.interaction_id) == other_message
        assert InteractionStore(copy).get(message).content == ERASED
        assert ModelLedger(copy).get_call(call_id).response == ERASED_MODEL
        assert ModelLedger(copy).attempts(call_id)[0].cost_microusd == 5
        assert SleepEngine(copy, "owner").get(sleep_id) == run
        assert SleepEngine(copy, "owner").lifecycle.current().state == "active"
        assert SleepIntegrity(copy).verify("owner") == integrity
        assert EventStore(copy).verify_chain("owner") == chain
        with pytest.raises(InteractionStateConflictError, match="reflection was erased"):
            PublicDiaryStore(copy).publish("owner", sleep_id, "revived", "old diary body")
        with copy.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sleep_reflections SET summary='revived' WHERE sleep_id=?", (sleep_id,)
            )
        with copy.read_transaction() as connection:
            assert connection.execute(
                "SELECT job_id,original_hash,erased_hash FROM private_erasure_receipts "
                "WHERE record_table='sleep_reflections' AND record_id=?",
                (original["reflection_id"],),
            ).fetchone()[:] == (result["job_id"], original["plan_hash"], ERASED_REFLECTION_HASH)
    assert RuntimeLogExporter(restored[-1]).export("owner", actor="fixture").row_count > 0


def test_sleep_reflection_erasure_is_atomic_on_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, event = _private_message(db)
    sleep_id, call_id, memory_id = _integrated_sleep_memory(db, message, event, "rollback fixture")
    original_memory = MemoryStore(db).get(memory_id)
    original_revisions = MemoryStore(db).revisions(memory_id)
    original = _reflection_row(db, sleep_id)
    call = ModelLedger(db).get_call(call_id)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)

    def fail_verification(connection: sqlite3.Connection, *_: Any) -> None:
        assert (
            connection.execute(
                "SELECT plan_hash FROM sleep_reflections WHERE sleep_id=?", (sleep_id,)
            ).fetchone()[0]
            == ERASED_REFLECTION_HASH
        )
        assert (
            connection.execute(
                "SELECT content FROM memories WHERE memory_id=?", (memory_id,)
            ).fetchone()[0]
            == ERASED
        )
        raise IntegrityError("fixture post-write verification failure")

    with monkeypatch.context() as failure:
        failure.setattr(
            RecoveryErasure, "_verify_reflection_receipts", staticmethod(fail_verification)
        )
        result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "failed" and result["error_code"] == "erasure_storage_error"
    assert _reflection_row(db, sleep_id) == original
    assert MemoryStore(db).get(memory_id) == original_memory
    assert MemoryStore(db).revisions(memory_id) == original_revisions
    assert ModelLedger(db).get_call(call_id) == call
    assert InteractionStore(db).get(message).content == "private original"
    with db.read_transaction() as connection:
        assert not connection.execute("SELECT 1 FROM private_erasure_receipts").fetchone()
        assert _row(connection, "owner", window["conversation_id"])["status"] == "deleting"
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    assert MemoryStore(db).get(memory_id).content == ERASED


@pytest.mark.parametrize(
    "case",
    [
        "missing_binding",
        "other_window",
        "open_run",
        "invalid_plan",
        "independent_mismatch",
        "diary",
        "runtime_export",
    ],
)
def test_sleep_reflection_erasure_rejects_unreviewed_scope(tmp_path: Path, case: str) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    inputs: tuple[str, ...] = (message,)
    if case in {"other_window", "independent_mismatch"}:
        second = ConversationStore(db).create("owner", "second window")
        other = InteractionStore(db).receive(
            "owner",
            "web",
            "web-user",
            "independent input",
            conversation_id=second["conversation_id"],
        )
        if case == "other_window":
            inputs += (other.interaction_id,)
        else:
            _sleep_reflection(db, (other.interaction_id,), "other plan", match_response=False)
    sleep_id, call_id = _sleep_reflection(
        db,
        inputs,
        "review fixture",
        bound=case != "missing_binding",
        complete=case != "open_run",
        match_response=case != "invalid_plan",
    )
    if case == "diary":
        PublicDiaryStore(db).publish("owner", sleep_id, "fixture", "review fixture diary candidate")
    if case == "runtime_export":
        RuntimeLogExporter(db).export_to_path(
            "owner", actor="fixture", target=tmp_path / "runtime.zip"
        )
    before = _reflection_row(db, sleep_id)
    call = ModelLedger(db).get_call(call_id)
    reason = {
        "other_window": "cross_window_derivative_review_required",
        "runtime_export": "private_legacy_export_review_required",
    }.get(case, "reflection_derivative_review_required")
    erasure = ConversationErasure(db)
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == reason
    with pytest.raises(ErasureReviewRequired, match=reason):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert _reflection_row(db, sleep_id) == before
    assert ModelLedger(db).get_call(call_id) == call
    assert InteractionStore(db).get(message).content == "private original"


def test_sleep_reflection_erasure_fences_unmanaged_runtime_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    _sleep_reflection(db, (message,), "reflection included in stale export")
    erasure = ConversationErasure(db)
    file_hash = RuntimeLogExporter._file_hash

    def finish_erasure(path: Path, **kwargs: Any) -> str:
        digest = file_hash(path, **kwargs)
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
        result = erasure.run("owner", window["conversation_id"])
        assert result["status"] == "completed", result
        return digest

    monkeypatch.setattr(RuntimeLogExporter, "_file_hash", staticmethod(finish_erasure))
    destination = tmp_path / "runtime.zip"
    with pytest.raises(ExportCancelledError, match="private content changed"):
        RuntimeLogExporter(db).export_to_path("owner", actor="fixture", target=destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".runtime.zip.*"))


def test_completed_sleep_reflection_restore_rejects_retained_projection(tmp_path: Path) -> None:
    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    sleep_id, _ = _sleep_reflection(db, (message,), "residual fixture")
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    # Model historical corruption in the fixture, retaining the reviewed schema.
    with db.transaction() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prevent_sleep_reflection_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER prevent_sleep_reflection_update")
        connection.execute(
            "UPDATE sleep_reflections SET unresolved_questions_json=? WHERE sleep_id=?",
            (canonical_json(["old private question"]), sleep_id),
        )
        connection.execute(trigger)
    db.assert_schema_contract()
    with pytest.raises(IntegrityError, match="sleep reflection residual"):
        SleepIntegrity(db).verify("owner")
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(root, keyring)
    backup = tmp_path / "damaged-backup.noyra-backup"
    manager.create(backup)
    destination = tmp_path / "restored"
    with pytest.raises(IntegrityError, match="sleep reflection residual"):
        manager.restore(backup, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".noyra-restore-*"))


def _integrated_sleep_memory(
    db: Database, message: str, event: str, text: str
) -> tuple[str, str, str]:
    sleep_id, call_id = _sleep_reflection(
        db,
        (message,),
        "fixture integration",
        memories=(
            SleepMemory(content=text, salience=0.4, confidence=0.7, source_event_ids=(event,)),
        ),
    )
    with db.read_transaction() as connection:
        memory_id = connection.execute(
            "SELECT target_id FROM sleep_integrations WHERE sleep_id=?", (sleep_id,)
        ).fetchone()[0]
    return sleep_id, call_id, memory_id


def test_exclusive_sleep_memory_erasure_clears_encrypted_backup_replays(tmp_path: Path) -> None:
    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    window, message, event = _private_message(db)
    original = "originalsleepmemorysecretfixture"
    revised = "revisedsleepmemorysecretfixture"
    reason = "revisionsleepmemorysecretfixture"
    sleep_id, call_id, memory_id = _integrated_sleep_memory(db, message, event, original)
    memories = MemoryStore(db)
    memories.revise(
        memory_id,
        content=revised,
        salience=0.5,
        confidence=0.8,
        status="active",
        reason=reason,
        source_event_ids=(event,),
    )
    other_event = EventStore(db).append("owner", "experience", "test", {}).event_id
    independent = memories.create(
        "owner",
        "reflection",
        "independent searchable memory",
        salience=0.4,
        confidence=0.7,
        source_event_ids=(other_event,),
    )
    with db.transaction() as connection:
        for target_id, body in ((memory_id, revised), (independent.memory_id, independent.content)):
            connection.execute(
                "INSERT INTO memory_embeddings VALUES(?,?,'mock',2,?,?,?)",
                (
                    target_id,
                    "owner",
                    canonical_json([0.2, 0.4]),
                    content_hash([0.2, 0.4]),
                    utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO memory_embedding_inputs VALUES (?,?)", (target_id, content_hash(body))
            )
        binding = dict(
            connection.execute(
                "SELECT * FROM sleep_integrations WHERE sleep_id=?", (sleep_id,)
            ).fetchone()
        )
        assert (
            connection.execute(
                "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", (revised,)
            ).fetchone()[0]
            == memory_id
        )
    keys = tmp_path / "keyring.json"
    BackupKeyring.initialize(keys)
    manager = EncryptedBackupManager(root, keys)
    before = tmp_path / "before.noyra-backup"
    manager.create(before)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    assert result["counts"]["sleep_memories"] == result["counts"]["exclusive_memories"] == 1
    after = tmp_path / "after.noyra-backup"
    manager.create(after)

    for index, backup in enumerate((before, after)):
        destination = manager.restore(backup, tmp_path / f"restored-{index}")
        copy = Database(
            destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
        )
        assert all(
            text.encode() not in copy.path.read_bytes() for text in (original, revised, reason)
        )
        assert SleepIntegrity(copy).verify("owner")["sleep_integrations"] == 1
        assert MindEngine(copy).verify_integrity("owner")["memories"] == 2
        restored = MemoryStore(copy)
        assert restored.get(independent.memory_id) == independent
        assert restored.get(memory_id).content == ERASED
        assert restored.get(memory_id).status == "archived"
        assert len(restored.revisions(memory_id)) == 3
        assert {revision["content"] for revision in restored.revisions(memory_id)} == {ERASED}
        assert {revision["reason"] for revision in restored.revisions(memory_id)} == {
            "privacy erasure"
        }
        assert ModelLedger(copy).get_call(call_id).response == ERASED_MODEL
        assert _reflection_row(copy, sleep_id)["plan_hash"] == ERASED_REFLECTION_HASH
        with copy.read_transaction() as connection:
            assert (
                dict(
                    connection.execute(
                        "SELECT * FROM sleep_integrations WHERE sleep_id=?", (sleep_id,)
                    ).fetchone()
                )
                == binding
            )
            assert (
                connection.execute(
                    "SELECT original_hash FROM private_erasure_receipts WHERE record_table="
                    "'sleep_integrations' AND record_id=? AND job_id=?",
                    (binding["integration_id"], result["job_id"]),
                ).fetchone()[0]
                == binding["result_hash"]
            )
            assert [
                row[0] for row in connection.execute("SELECT memory_id FROM memory_embeddings")
            ] == [independent.memory_id]
            assert [
                row[0]
                for row in connection.execute("SELECT memory_id FROM memory_embedding_inputs")
            ] == [independent.memory_id]
            assert not connection.execute(
                "SELECT 1 FROM memory_fts WHERE memory_fts MATCH ?", (revised,)
            ).fetchone()
            assert (
                connection.execute(
                    "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH 'searchable'"
                ).fetchone()[0]
                == independent.memory_id
            )


@pytest.mark.parametrize(
    "case", ["uncaptured_input", "mixed_revision", "bad_receipt", "other_sleep"]
)
def test_sleep_memory_erasure_rejects_unproven_lineage(tmp_path: Path, case: str) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, event = _private_message(db)
    if case == "uncaptured_input":
        additional = InteractionStore(db).receive(
            "owner",
            "web",
            "web-user",
            "uncaptured input",
            conversation_id=window["conversation_id"],
        )
        with db.read_transaction() as connection:
            event = connection.execute(
                "SELECT event_id FROM events WHERE subject_id='owner' "
                "AND event_type='interaction_received' "
                "AND json_extract(payload_json,'$.interaction_id')=?",
                (additional.interaction_id,),
            ).fetchone()[0]
    source_message = message
    if case == "other_sleep":
        other_window = ConversationStore(db).create("owner", "separate window")
        source_message = (
            InteractionStore(db)
            .receive(
                "owner",
                "web",
                "web-user",
                "separate input",
                conversation_id=other_window["conversation_id"],
            )
            .interaction_id
        )
    sleep_id, call_id, memory_id = _integrated_sleep_memory(
        db, source_message, event, "fixture memory"
    )
    if case == "mixed_revision":
        other_event = EventStore(db).append("owner", "experience", "test", {}).event_id
        MemoryStore(db).revise(
            memory_id,
            content="mixed revision",
            salience=0.5,
            confidence=0.8,
            status="active",
            reason="fixture",
            source_event_ids=(event, other_event),
        )
    if case == "bad_receipt":
        _replace_sleep_integration_hash(db, sleep_id)
    original = MemoryStore(db).revisions(memory_id)
    reflection = _reflection_row(db, sleep_id)
    call = ModelLedger(db).get_call(call_id)
    erasure = ConversationErasure(db)
    reason_code = "reflection_derivative_review_required"
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == reason_code
    with pytest.raises(ErasureReviewRequired, match=reason_code):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).revisions(memory_id) == original
    assert _reflection_row(db, sleep_id) == reflection
    assert ModelLedger(db).get_call(call_id) == call


@pytest.mark.parametrize("reverted", [False, True])
def test_private_contradiction_integration_reason_and_output_are_erased_with_backups(
    tmp_path: Path, reverted: bool
) -> None:
    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    _sleep_id, call_id, private_memory_id = _integrated_sleep_memory(
        db, message, private_event, "private source memory for contradiction"
    )
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "independent source evidence"}
    )
    independent = MemoryStore(db).create(
        "owner",
        "semantic",
        "independent source memory",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    synthesis = "private contradiction synthesis must be removed"
    private_reason = "private user's detailed conflict reason must be removed"
    private_revert_reason = "private subsequent reversal rationale"
    integration = MemoryIntegrationSupervisor(db, "owner").integrate(
        "contradict",
        (private_memory_id, independent.memory_id),
        synthesis=synthesis,
        confidence=0.4,
        evidence_event_ids=(independent_event.event_id,),
        reason=private_reason,
    )
    with db.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE memory_integrations SET reason='unauthorized edit' WHERE integration_id=?",
            (integration.integration_id,),
        )
    with db.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE memory_integration_revisions SET reason='unauthorized edit' "
            "WHERE integration_id=?",
            (integration.integration_id,),
        )
    if reverted:
        integration = MemoryIntegrationSupervisor(db, "owner").revert(
            integration.integration_id, reason="private subsequent reversal rationale"
        )
    with db.read_transaction() as connection:
        original = dict(
            connection.execute(
                "SELECT * FROM memory_integrations WHERE integration_id=?",
                (integration.integration_id,),
            ).fetchone()
        )
        original_revisions = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memory_integration_revisions WHERE integration_id=?",
                (integration.integration_id,),
            )
        ]
        output_id = integration.output_memory_id
    assert output_id is not None
    assert MemoryStore(db).get(output_id).content == synthesis
    keys = tmp_path / "keyring.json"
    BackupKeyring.initialize(keys)
    manager = EncryptedBackupManager(root, keys)
    before = tmp_path / "before-private-integration-erasure.noyra-backup"
    manager.create(before)

    erasure = ConversationErasure(db)
    result = erasure.assess("owner", window["conversation_id"])
    assert result["status"] == "full_contract_pending"
    assert result["deletion_available"] is False
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    completed = erasure.run("owner", window["conversation_id"])
    assert completed["status"] == "completed", completed
    assert completed["counts"]["memory_integration_records"] == 1
    assert completed["counts"]["memory_integration_revisions"] == len(original_revisions)
    assert completed["counts"]["discarded_mixed_memories"] == 1
    assert MemoryStore(db).get(output_id).content == ERASED
    assert MemoryStore(db).get(independent.memory_id).content == independent.content
    assert MemoryStore(db).get(independent.memory_id).status == independent.status
    after = tmp_path / "after-private-integration-erasure.noyra-backup"
    manager.create(after)

    for index, backup in enumerate((before, after)):
        destination = manager.restore(backup, tmp_path / f"restored-private-integration-{index}")
        copy = Database(
            destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
        )
        raw = copy.path.read_bytes()
        assert synthesis.encode() not in raw
        assert private_reason.encode() not in raw
        if reverted:
            assert private_revert_reason.encode() not in raw
        assert MemoryStore(copy).get(output_id).content == ERASED
        assert MemoryStore(copy).get(independent.memory_id) == independent
        assert ModelLedger(copy).get_call(call_id).response == ERASED_MODEL
        assert SleepIntegrity(copy).verify("owner")["sleep_runs"] == 1
        assert MemoryStore(copy).verify_lifecycle_integrity("owner")["memory_integrations"] == 1
        with copy.read_transaction() as connection:
            redacted = dict(
                connection.execute(
                    "SELECT * FROM memory_integrations WHERE integration_id=?",
                    (integration.integration_id,),
                ).fetchone()
            )
            redacted_revisions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM memory_integration_revisions WHERE integration_id=?",
                    (integration.integration_id,),
                )
            ]
            assert redacted["reason"] == ERASED
            assert redacted["source_memory_ids_json"] == original["source_memory_ids_json"]
            assert redacted["output_memory_id"] == original["output_memory_id"]
            assert len(redacted_revisions) == len(original_revisions)
            assert all(revision["reason"] == ERASED for revision in redacted_revisions)
            receipt_targets = [
                ("memory_integrations", integration.integration_id),
                *(
                    ("memory_integration_revisions", revision["revision_id"])
                    for revision in original_revisions
                ),
            ]
            for table, record_id in receipt_targets:
                receipt = connection.execute(
                    "SELECT original_hash,erased_hash FROM private_erasure_receipts "
                    "WHERE job_id=? AND record_table=? AND record_id=?",
                    (completed["job_id"], table, record_id),
                ).fetchone()
                assert receipt is not None
                assert receipt["original_hash"] != receipt["erased_hash"]
        with copy.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE memory_integrations SET reason='revived' WHERE integration_id=?",
                (integration.integration_id,),
            )
        for revision in original_revisions:
            with copy.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE memory_integration_revisions SET reason='revived' WHERE revision_id=?",
                    (revision["revision_id"],),
                )


def test_contradiction_integration_chain_is_erased_to_a_verified_fixed_point(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    _, call_id, private_memory_id = _integrated_sleep_memory(
        db, message, private_event, "private memory chain root"
    )
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "independent chain source"}
    )
    independent = MemoryStore(db).create(
        "owner",
        "semantic",
        "independent chain source memory",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    supervisor = MemoryIntegrationSupervisor(db, "owner")
    first = supervisor.integrate(
        "contradict",
        (private_memory_id, independent.memory_id),
        synthesis="first private chain output",
        confidence=0.4,
        evidence_event_ids=(independent_event.event_id,),
        reason="first private chain reason",
    )
    assert first.output_memory_id is not None
    second = supervisor.integrate(
        "contradict",
        (first.output_memory_id, independent.memory_id),
        synthesis="second private chain output",
        confidence=0.35,
        evidence_event_ids=(independent_event.event_id,),
        reason="second private chain reason",
    )
    assert second.output_memory_id is not None
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    assert result["counts"]["memory_integration_records"] == 2
    assert result["counts"]["memory_integration_revisions"] == 2
    assert result["counts"]["discarded_mixed_memories"] == 2
    for integration in (first, second):
        assert integration.output_memory_id is not None
        assert MemoryStore(db).get(integration.output_memory_id).content == ERASED
    assert MemoryStore(db).get(independent.memory_id) == independent
    assert MemoryStore(db).get(private_memory_id).content == ERASED
    assert MemoryStore(db).verify_lifecycle_integrity("owner")["memory_integrations"] == 2
    assert ModelLedger(db).get_call(call_id).response == ERASED_MODEL


def test_contradiction_integration_erasure_verification_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    _, call_id, private_memory_id = _integrated_sleep_memory(
        db, message, private_event, "rollback private sleep memory"
    )
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "independent"}
    )
    independent = MemoryStore(db).create(
        "owner",
        "semantic",
        "rollback independent source",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    synthesis = "rollback private contradiction output"
    integration = MemoryIntegrationSupervisor(db, "owner").integrate(
        "contradict",
        (private_memory_id, independent.memory_id),
        synthesis=synthesis,
        confidence=0.4,
        evidence_event_ids=(independent_event.event_id,),
        reason="rollback private integration rationale",
    )
    output_id = integration.output_memory_id
    assert output_id is not None
    with db.read_transaction() as connection:
        integration_before = dict(
            connection.execute(
                "SELECT * FROM memory_integrations WHERE integration_id=?",
                (integration.integration_id,),
            ).fetchone()
        )
        revisions_before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memory_integration_revisions WHERE integration_id=?",
                (integration.integration_id,),
            )
        ]
    output_before = MemoryStore(db).get(output_id)
    private_before = MemoryStore(db).get(private_memory_id)
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)

    def fail_after_redaction(connection: sqlite3.Connection, *args: Any) -> None:
        row = connection.execute(
            "SELECT reason FROM memory_integrations WHERE integration_id=?",
            (integration.integration_id,),
        ).fetchone()
        assert row["reason"] == ERASED
        assert (
            connection.execute(
                "SELECT content FROM memories WHERE memory_id=?", (output_id,)
            ).fetchone()[0]
            == ERASED
        )
        raise IntegrityError("injected post-integration verification failure")

    with monkeypatch.context() as failure:
        failure.setattr(
            RecoveryErasure,
            "_verify_memory_integration_receipts",
            staticmethod(fail_after_redaction),
        )
        failed = erasure.run("owner", window["conversation_id"])
    assert failed["status"] == "failed" and failed["error_code"] == "erasure_storage_error"
    assert MemoryStore(db).get(output_id) == output_before
    assert MemoryStore(db).get(private_memory_id) == private_before
    with db.read_transaction() as connection:
        assert (
            dict(
                connection.execute(
                    "SELECT * FROM memory_integrations WHERE integration_id=?",
                    (integration.integration_id,),
                ).fetchone()
            )
            == integration_before
        )
        assert [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memory_integration_revisions WHERE integration_id=?",
                (integration.integration_id,),
            )
        ] == revisions_before
        assert not connection.execute(
            "SELECT 1 FROM private_erasure_receipts WHERE record_table LIKE 'memory_integration%'"
        ).fetchone()
        assert _row(connection, "owner", window["conversation_id"])["status"] == "deleting"
    assert InteractionStore(db).get(message).content == "private original"
    assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    assert MemoryStore(db).get(output_id).content == ERASED
    assert ModelLedger(db).get_call(call_id).response == ERASED_MODEL


def test_unverified_contradiction_integration_requires_review_without_marker(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    _, _, private_memory_id = _integrated_sleep_memory(
        db, message, private_event, "private integration parent"
    )
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "independent"}
    )
    independent = MemoryStore(db).create(
        "owner",
        "semantic",
        "independent integration parent",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    integration = MemoryIntegrationSupervisor(db, "owner").integrate(
        "contradict",
        (private_memory_id, independent.memory_id),
        synthesis="mixed output",
        confidence=0.4,
        evidence_event_ids=(independent_event.event_id,),
        reason="fixture reason",
    )
    with db.transaction() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prevent_memory_integration_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER prevent_memory_integration_update")
        connection.execute(
            "UPDATE memory_integrations SET evidence_event_ids_json=? WHERE integration_id=?",
            (canonical_json(["missing-evidence-event"]), integration.integration_id),
        )
        connection.execute(trigger)
    erasure = ConversationErasure(db)
    with pytest.raises(IntegrityError, match=r"memory integration .* state hash mismatch"):
        erasure.assess("owner", window["conversation_id"])
    assert not erasure.root.exists()
    assert InteractionStore(db).get(message).content == "private original"


def test_schema120_upgrade_preserves_integrations_and_allows_audited_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "integration-v120.sqlite3"
    with monkeypatch.context() as old_schema:
        old_schema.setattr(database_module, "CURRENT_SCHEMA_VERSION", 120)
        db = Database(path)
        window, _, _ = _private_message(db)
        event = EventStore(db).append("owner", "experience", "fixture", {"safe": True})
        source_ids = tuple(
            MemoryStore(db)
            .create(
                "owner",
                "semantic",
                f"schema source {index}",
                salience=0.4,
                confidence=0.6,
                source_event_ids=(event.event_id,),
            )
            .memory_id
            for index in range(2)
        )
        integration = MemoryIntegrationSupervisor(db, "owner").integrate(
            "contradict",
            source_ids,
            synthesis="preserved pre-migration output",
            confidence=0.4,
            evidence_event_ids=(event.event_id,),
            reason="pre-migration reason",
        )
        before = MemoryStore(db).verify_lifecycle_integrity("owner")
        with db.read_transaction() as connection:
            row_before = dict(
                connection.execute(
                    "SELECT * FROM memory_integrations WHERE integration_id=?",
                    (integration.integration_id,),
                ).fetchone()
            )
    upgraded = Database(path, strict_schema_contract=True)
    with upgraded.read_transaction() as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == str(database_module.CURRENT_SCHEMA_VERSION)
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='prevent_memory_integration_update'"
            ).fetchone()
            is not None
        )
        row_after = dict(
            connection.execute(
                "SELECT * FROM memory_integrations WHERE integration_id=?",
                (integration.integration_id,),
            ).fetchone()
        )
    assert row_after == row_before
    with upgraded.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE memory_integrations SET reason='migration bypass' WHERE integration_id=?",
            (integration.integration_id,),
        )
    reverted = MemoryIntegrationSupervisor(upgraded, "owner").revert(
        integration.integration_id, reason="audited post-migration reversal"
    )
    assert reverted.status == "reverted"
    assert (
        MemoryStore(upgraded).verify_lifecycle_integrity("owner")["memory_integrations"]
        == (before["memory_integrations"])
    )
    assert (
        ConversationStore(upgraded).list("owner")["items"][0]["conversation_id"]
        == (window["conversation_id"])
    )


@pytest.mark.parametrize("operation", ["merge", "supersede"])
def test_memory_integration_merge_and_supersede_remain_fail_closed(
    tmp_path: Path, operation: str
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    _, _, private_memory_id = _integrated_sleep_memory(
        db, message, private_event, "private source memory"
    )
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "independent evidence"}
    )
    independent = MemoryStore(db).create(
        "owner",
        "semantic",
        "independent source memory",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    integration = MemoryIntegrationSupervisor(db, "owner").integrate(
        operation,
        (private_memory_id, independent.memory_id),
        synthesis="mixed output",
        confidence=0.4,
        evidence_event_ids=(independent_event.event_id,),
        reason="fixture reason",
    )
    assert integration.output_memory_id is not None
    output = MemoryStore(db).get(integration.output_memory_id)
    erasure = ConversationErasure(db)
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == (
        "memory_integration_review_required"
    )
    with pytest.raises(ErasureReviewRequired, match="memory_integration_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).get(output.memory_id) == output
    assert InteractionStore(db).get(message).content == "private original"


@pytest.mark.parametrize("archived", [False, True])
def test_sleep_reflection_mixed_memory_rebuilds_only_pre_window_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archived: bool
) -> None:
    from noyra.core.events import EventStore

    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior_event:
        prior_event.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        independent_event = EventStore(db).append(
            "owner", "independent_experience", "fixture", {"text": "stable older evidence"}
        )
    original_memory_content = "pre window sleep model substrate"
    memory = MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00").create(
        "owner",
        "episodic",
        original_memory_content,
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    window, message, _ = _private_message(db)
    sleep_id, call_id = _sleep_reflection(db, (message,), "mixed downstream")
    reflection_event = SleepEngine(db, "owner").get(sleep_id).reflection_event_id
    assert reflection_event is not None
    mixed_content = "later content influenced by private sleep reflection"
    MemoryStore(db).revise(
        memory.memory_id,
        content=mixed_content,
        salience=0.8,
        confidence=0.9,
        status="archived" if archived else "active",
        reason="sleep-derived synthesis",
        source_event_ids=(reflection_event, independent_event.event_id),
    )
    before_revisions = MemoryStore(db).revisions(memory.memory_id)
    assert len(before_revisions) == 2
    assert before_revisions[1]["content"] == mixed_content
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(root, keyring)
    before = tmp_path / "before-mixed-erasure.noyra-backup"
    manager.create(before)

    erasure = ConversationErasure(db)
    assessment = erasure.assess("owner", window["conversation_id"])
    assert assessment["status"] == "full_contract_pending"
    assert assessment["deletion_available"] is False
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    assert result["counts"]["reconstructed_memories"] == 1
    assert result["counts"]["exclusive_memories"] == 0
    assert result["counts"].get("sleep_memories", 0) == 0
    assert MemoryStore(db).get(memory.memory_id).content == ERASED
    after = tmp_path / "after-mixed-erasure.noyra-backup"
    manager.create(after)

    for index, backup in enumerate((before, after)):
        destination = manager.restore(backup, tmp_path / f"restored-mixed-{index}")
        copy = Database(
            destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
        )
        assert mixed_content.encode() not in copy.path.read_bytes()
        assert original_memory_content.encode() in copy.path.read_bytes()
        assert MemoryStore(copy).get(memory.memory_id).content == ERASED
        assert all(
            revision["content"] == ERASED and revision["reason"] == "privacy erasure"
            for revision in MemoryStore(copy).revisions(memory.memory_id)
        )
        with copy.read_transaction() as connection:
            replacements = connection.execute(
                "SELECT * FROM memories WHERE memory_id!=? AND subject_id='owner'",
                (memory.memory_id,),
            ).fetchall()
            assert len(replacements) == 1
            replacement = replacements[0]
            assert replacement["content"] == original_memory_content
            assert replacement["status"] == ("archived" if archived else "active")
            assert MemoryStore(copy).source_event_ids(replacement["memory_id"]) == (
                independent_event.event_id,
            )
            assert not connection.execute(
                "SELECT 1 FROM memory_fts WHERE memory_fts MATCH ?", (mixed_content,)
            ).fetchone()
        assert ModelLedger(copy).get_call(call_id).response == ERASED_MODEL
        assert MindEngine(copy).verify_integrity("owner")["memories"] == 2
        assert SleepIntegrity(copy).verify("owner")["sleep_runs"] == 1


@pytest.mark.parametrize("duplicate_keeper", [False, True])
def test_sleep_memory_consolidation_descendants_are_erased_and_backups_replay(
    tmp_path: Path, duplicate_keeper: bool
) -> None:
    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "older independent fact"}
    )
    memory_text = (
        "independent duplicate payload" if duplicate_keeper else "independent cluster fact"
    )
    independent = MemoryStore(db).create(
        "owner",
        "reflection",
        memory_text,
        salience=0.2 if duplicate_keeper else 0.25,
        confidence=0.5,
        source_event_ids=(independent_event.event_id,),
    )
    window, message, private_event = _private_message(db)
    sleep_memory_text = memory_text if duplicate_keeper else "private sleep-derived cluster member"
    sleep_id, call_id = _sleep_reflection(
        db,
        (message,),
        "consolidation lineage",
        memories=(
            SleepMemory(
                content=sleep_memory_text,
                memory_type="reflection",
                salience=0.15,
                confidence=0.4,
                source_event_ids=(private_event,),
            ),
        ),
    )
    with db.read_transaction() as connection:
        private_memory_id = connection.execute(
            "SELECT target_id FROM sleep_integrations WHERE sleep_id=? "
            "AND integration_type='memory'",
            (sleep_id,),
        ).fetchone()[0]
    private_memory = MemoryStore(db).get(private_memory_id)
    if duplicate_keeper:
        assert private_memory.memory_type == independent.memory_type
        assert private_memory.content == independent.content
    consolidator = MemoryConsolidator(
        db,
        "owner",
        interval_seconds=0,
        stale_after_days=0,
        archive_after_days=0,
        minimum_active_memories=0,
    )
    assert consolidator.run_due() == "memory_consolidation_committed"
    with db.read_transaction() as connection:
        run = dict(
            connection.execute(
                "SELECT * FROM memory_consolidation_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        )
        members = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memory_consolidation_members "
                "WHERE consolidation_id=? ORDER BY rowid",
                (run["consolidation_id"],),
            )
        ]
        summary_ids = json.loads(run["summary_memory_ids_json"])
        summaries = [
            dict(
                connection.execute(
                    "SELECT * FROM memories WHERE memory_id=?", (memory_id,)
                ).fetchone()
            )
            for memory_id in summary_ids
        ]
    assert any(row["source_memory_id"] == private_memory_id for row in members)
    if duplicate_keeper:
        assert (
            next(row for row in members if row["source_memory_id"] == private_memory_id)[
                "disposition"
            ]
            == "archived_duplicate"
        )
    else:
        assert summaries and all(
            row["content"].startswith("Consolidated memory cluster: ") for row in summaries
        )
    keys = tmp_path / "keyring.json"
    BackupKeyring.initialize(keys)
    manager = EncryptedBackupManager(root, keys)
    before = tmp_path / "before-consolidation-erasure.noyra-backup"
    manager.create(before)

    erasure = ConversationErasure(db)
    assessment = erasure.assess("owner", window["conversation_id"])
    assert assessment["status"] == "full_contract_pending"
    assert assessment["deletion_available"] is False
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    after = tmp_path / "after-consolidation-erasure.noyra-backup"
    manager.create(after)

    for index, backup in enumerate((before, after)):
        destination = manager.restore(backup, tmp_path / f"restored-consolidation-{index}")
        copy = Database(
            destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
        )
        assert b"private sleep-derived cluster member" not in copy.path.read_bytes()
        assert SleepIntegrity(copy).verify("owner")["sleep_runs"] == 1
        assert MindEngine(copy).verify_integrity("owner")["memories"] >= 2
        assert MemoryStore(copy).get(private_memory_id).content == ERASED
        assert all(
            revision["content"] == ERASED
            for revision in MemoryStore(copy).revisions(private_memory_id)
        )
        assert ModelLedger(copy).get_call(call_id).response == ERASED_MODEL
        if duplicate_keeper:
            assert MemoryStore(copy).get(independent.memory_id).content == ERASED
            with copy.read_transaction() as connection:
                retained = connection.execute(
                    "SELECT memory_id,status FROM memories WHERE subject_id=? AND content=? "
                    "AND memory_id NOT IN (?,?)",
                    ("owner", memory_text, private_memory_id, independent.memory_id),
                ).fetchall()
            assert len(retained) == 1
            assert retained[0]["status"] == "archived"
            assert MemoryStore(copy).source_event_ids(retained[0]["memory_id"]) == (
                independent_event.event_id,
            )
        else:
            assert MemoryStore(copy).get(independent.memory_id).status == "archived"
            assert MemoryStore(copy).get(independent.memory_id).content == memory_text
        with copy.read_transaction() as connection:
            assert (
                dict(
                    connection.execute(
                        "SELECT * FROM memory_consolidation_runs WHERE consolidation_id=?",
                        (run["consolidation_id"],),
                    ).fetchone()
                )
                == run
            )
            restored_members = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM memory_consolidation_members "
                    "WHERE consolidation_id=? ORDER BY rowid",
                    (run["consolidation_id"],),
                )
            ]
            assert restored_members == members
            for summary in summaries:
                assert (
                    connection.execute(
                        "SELECT content FROM memories WHERE memory_id=?", (summary["memory_id"],)
                    ).fetchone()[0]
                    == ERASED
                )


def test_reused_consolidation_summary_without_new_safe_revision_requires_review(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"fact": "unrelated old evidence"}
    )
    sleep_id, _ = _sleep_reflection(
        db,
        (message,),
        "legacy summary reuse",
        memories=(
            SleepMemory(
                content="private member in reused summary",
                memory_type="reflection",
                salience=0.15,
                confidence=0.4,
                source_event_ids=(private_event,),
            ),
        ),
    )
    with db.read_transaction() as connection:
        private_memory_id = connection.execute(
            "SELECT target_id FROM sleep_integrations WHERE sleep_id=?", (sleep_id,)
        ).fetchone()[0]
    independent = MemoryStore(db).create(
        "owner",
        "reflection",
        "unrelated old consolidation member",
        salience=0.2,
        confidence=0.4,
        source_event_ids=(independent_event.event_id,),
    )
    cluster = tuple(sorted((private_memory_id, independent.memory_id)))
    reused_summary = MemoryStore(db).create(
        "owner",
        "reflection",
        "Consolidated memory cluster: " + ", ".join(cluster),
        salience=0.45,
        confidence=0.45,
        source_event_ids=(independent_event.event_id,),
        reason="fixture legacy summary",
    )
    assert (
        MemoryConsolidator(
            db,
            "owner",
            interval_seconds=0,
            stale_after_days=0,
            archive_after_days=0,
            minimum_active_memories=0,
        ).run_due()
        == "memory_consolidation_committed"
    )
    with db.read_transaction() as connection:
        run = connection.execute(
            "SELECT summary_memory_ids_json FROM memory_consolidation_runs "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert json.loads(run[0]) == []
        assert connection.execute(
            "SELECT 1 FROM memory_consolidation_members WHERE source_memory_id=? "
            "AND result_memory_id=?",
            (private_memory_id, reused_summary.memory_id),
        ).fetchone()
    before = MemoryStore(db).get(reused_summary.memory_id)
    erasure = ConversationErasure(db)
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == (
        "memory_consolidation_review_required"
    )
    with pytest.raises(ErasureReviewRequired, match="memory_consolidation_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).get(reused_summary.memory_id) == before
    assert InteractionStore(db).get(message).content == "private original"


def test_sleep_consolidation_tampered_membership_requires_review(tmp_path: Path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, private_event = _private_message(db)
    sleep_id, _ = _sleep_reflection(
        db,
        (message,),
        "tampered consolidation",
        memories=(
            SleepMemory(
                content="sleep memory for consolidation",
                memory_type="reflection",
                salience=0.2,
                confidence=0.4,
                source_event_ids=(private_event,),
            ),
        ),
    )
    with db.read_transaction() as connection:
        memory_id = connection.execute(
            "SELECT target_id FROM sleep_integrations WHERE sleep_id=?", (sleep_id,)
        ).fetchone()[0]
    assert (
        MemoryConsolidator(
            db,
            "owner",
            interval_seconds=0,
            stale_after_days=0,
            archive_after_days=0,
            minimum_active_memories=0,
        ).run_due()
        == "memory_consolidation_committed"
    )
    with db.transaction() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prevent_memory_consolidation_member_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER prevent_memory_consolidation_member_update")
        connection.execute(
            "UPDATE memory_consolidation_members SET reason='tampered free text' "
            "WHERE source_memory_id=?",
            (memory_id,),
        )
        connection.execute(trigger)
    before = MemoryStore(db).get(memory_id)
    erasure = ConversationErasure(db)
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == (
        "memory_consolidation_review_required"
    )
    with pytest.raises(ErasureReviewRequired, match="memory_consolidation_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).get(memory_id) == before
    assert InteractionStore(db).get(message).content == "private original"


def test_sleep_reflection_mixed_memory_without_pre_window_revision_requires_review(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    window, message, _ = _private_message(db)
    independent_event = EventStore(db).append(
        "owner", "independent_experience", "fixture", {"text": "later independent citation"}
    )
    sleep_id, call_id = _sleep_reflection(db, (message,), "no prior memory")
    reflection_event = SleepEngine(db, "owner").get(sleep_id).reflection_event_id
    assert reflection_event is not None
    memory = MemoryStore(db).create(
        "owner",
        "episodic",
        "memory born after private input",
        salience=0.4,
        confidence=0.6,
        source_event_ids=(independent_event.event_id,),
    )
    MemoryStore(db).revise(
        memory.memory_id,
        content="mixed after-window copy",
        salience=0.7,
        confidence=0.8,
        status="active",
        reason="sleep-derived synthesis",
        source_event_ids=(reflection_event, independent_event.event_id),
    )
    revision_snapshot = MemoryStore(db).revisions(memory.memory_id)
    erasure = ConversationErasure(db)
    assert erasure.assess("owner", window["conversation_id"])["reason_code"] == (
        "multi_source_memory_review_required"
    )
    with pytest.raises(ErasureReviewRequired, match="multi_source_memory_review_required"):
        erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    assert not erasure.root.exists()
    assert MemoryStore(db).revisions(memory.memory_id) == revision_snapshot
    assert _reflection_row(db, sleep_id)["plan_hash"] != ERASED_REFLECTION_HASH
    assert ModelLedger(db).get_call(call_id).response != ERASED_MODEL
    assert InteractionStore(db).get(message).content == "private original"


def _replace_sleep_integration_hash(db: Database, sleep_id: str) -> None:
    # Model an inconsistent historical copy while keeping the reviewed DDL.
    with db.transaction() as connection:
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prevent_sleep_integration_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER prevent_sleep_integration_update")
        connection.execute(
            "UPDATE sleep_integrations SET result_hash=? WHERE sleep_id=?", ("0" * 64, sleep_id)
        )
        connection.execute(guard)


@pytest.mark.parametrize("residual", ["binding", "vector_input", "search_index"])
def test_sleep_memory_erasure_refuses_inconsistent_completed_backup(
    tmp_path: Path, residual: str
) -> None:
    root = tmp_path / "current"
    db = Database(root / "noyra.sqlite3")
    window, message, event = _private_message(db)
    sleep_id, _, memory_id = _integrated_sleep_memory(db, message, event, "fixture memory")
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "completed", result
    if residual == "binding":
        _replace_sleep_integration_hash(db, sleep_id)
        error = "sleep memory evidence differs"
    elif residual == "vector_input":
        with db.connection() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO memory_embedding_inputs VALUES (?,?)",
                (memory_id, content_hash("old fixture vector input")),
            )
            connection.commit()
        error = "sleep memory embedding input residual"
    else:
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_fts(memory_id,subject_id,content) VALUES (?,'owner',?)",
                (memory_id, "stale fixture index"),
            )
        error = "private erasure index residual"
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(root, keyring)
    backup = tmp_path / "damaged-backup.noyra-backup"
    manager.create(backup)
    destination = tmp_path / "restored"
    with pytest.raises(IntegrityError, match=error):
        manager.restore(backup, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".noyra-restore-*"))


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
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior:
        prior.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        event_store = EventStore(db)
        independent = event_store.append("owner", "independent", "test", {"fact": "safe"})
        additional = tuple(
            event_store.append("owner", "independent", "test", {"sequence": index}).event_id
            for index in range(63)
        )
    memory = MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00").create(
        "owner",
        "episodic",
        "independent fact",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(independent.event_id,),
    )
    window, message, event = _private_message(db)
    # Both revisions fit the limit, but their combined sources exceed it.
    later_sources = (*additional, event)
    MemoryStore(db).revise(
        memory.memory_id,
        content="private mixed fact",
        salience=0.8,
        confidence=0.9,
        status="active",
        reason="private reason",
        source_event_ids=later_sources,
    )
    other = ConversationStore(db).create("owner", "independent window")
    independent_message = InteractionStore(db).receive(
        "owner",
        "web",
        "web-user",
        "another window's text",
        conversation_id=other["conversation_id"],
    )
    with db.connection() as connection:
        independent_event = connection.execute(
            "SELECT event_id FROM events WHERE subject_id='owner' "
            "AND event_type='interaction_received' "
            "AND json_extract(payload_json,'$.interaction_id')=?",
            (independent_message.interaction_id,),
        ).fetchone()[0]
    other_memory = MemoryStore(db).create(
        "owner",
        "semantic",
        "unrelated window fact",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(independent_event,),
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
    store = MemoryStore(restored)
    assert store.get(memory.memory_id).content == ERASED
    revisions = store.revisions(memory.memory_id)
    assert len(revisions) == 3
    assert all(row["content"] == ERASED for row in revisions)
    assert revisions[0]["source_event_ids_json"] == canonical_json([independent.event_id])
    assert revisions[1]["source_event_ids_json"] == canonical_json(later_sources)
    assert revisions[2]["source_event_ids_json"] == canonical_json(later_sources)
    assert store.get(other_memory.memory_id) == other_memory
    assert InteractionStore(restored).get(independent_message.interaction_id) == independent_message
    remaining = store.search("owner")
    assert {item.content for item in remaining} == {"independent fact", "unrelated window fact"}
    with restored.connection() as connection:
        assert not connection.execute(
            "SELECT 1 FROM memory_fts WHERE memory_id=?", (memory.memory_id,)
        ).fetchone()
    assert MindEngine(restored).verify_integrity("owner")["memories"] == 3


@pytest.mark.parametrize("case", ["legacy", "stale_index", "receipt_count_mismatch"])
def test_restore_rechecks_completed_memory_erasure_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    current = tmp_path / "current"
    db = Database(current / "noyra.sqlite3")
    window, _, event = _private_message(db)
    memory = MemoryStore(db).create(
        "owner",
        "semantic",
        "private text",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(event,),
    )
    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    erase_memories = RecoveryErasure._erase_affected_memories

    def erase_with_older_counts(self: RecoveryErasure, *args: Any) -> dict[str, int]:
        counts = erase_memories(self, *args)
        if case == "legacy":
            del counts["reconstructed_memories"]
        elif case == "receipt_count_mismatch":
            counts["exclusive_memories"] += 1
        return counts

    with monkeypatch.context() as legacy:
        legacy.setattr(RecoveryErasure, "_erase_affected_memories", erase_with_older_counts)
        assert erasure.run("owner", window["conversation_id"])["status"] == "completed"
    if case == "stale_index":
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_fts(memory_id,subject_id,content) VALUES(?,?,?)",
                (memory.memory_id, "owner", "stale completed-job text"),
            )
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "after-erasure.noyra-backup"
    manager.create(backup)
    target = tmp_path / "restored"
    if case != "legacy":
        reason = "private erasure index residual" if case == "stale_index" else "receipt counts"
        with pytest.raises(IntegrityError, match=reason):
            manager.restore(backup, target)
        assert not target.exists()
        assert not list(tmp_path.glob(".noyra-restore-*"))
    else:
        assert manager.restore(backup, target) == target
        copy = Database(target / "noyra.sqlite3")
        assert MemoryStore(copy).get(memory.memory_id).content == ERASED
        assert MemoryStore(copy).search("owner") == []


@pytest.mark.parametrize("residual", ["prior_revision", "search_index"])
def test_erasure_rolls_back_if_any_memory_copy_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residual: str
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    with monkeypatch.context() as prior:
        prior.setattr("noyra.core.events.utc_now", lambda: "1999-01-01T00:00:00.000+00:00")
        evidence = EventStore(db).append("owner", "independent", "test", {})
    memory = MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00").create(
        "owner",
        "semantic",
        "independent original",
        salience=0.5,
        confidence=0.7,
        source_event_ids=(evidence.event_id,),
    )
    window, message_id, private_event = _private_message(db)
    store = MemoryStore(db)
    store.revise(
        memory.memory_id,
        content="mixed private history",
        salience=0.4,
        confidence=0.6,
        status="active",
        reason="fixture revision",
        source_event_ids=(private_event,),
    )
    if residual == "prior_revision":
        # Simulate an update that reports no exception but misses a revision
        # that has no direct target event in its source list.
        with db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER test_skip_memory_redaction BEFORE UPDATE ON memory_revisions "
                "WHEN OLD.revision_number=1 BEGIN SELECT RAISE(IGNORE); END"
            )
    else:
        verify = RecoveryErasure._verify

        def verify_with_stale_index(connection: sqlite3.Connection, *args: Any) -> None:
            connection.execute(
                "INSERT INTO memory_fts(memory_id,subject_id,content) VALUES(?,?,?)",
                (memory.memory_id, "owner", "stale index text"),
            )
            verify(connection, *args)

        monkeypatch.setattr(RecoveryErasure, "_verify", staticmethod(verify_with_stale_index))

    erasure = ConversationErasure(db)
    erasure.request("owner", window["conversation_id"], expected_revision=1, confirmed=True)
    current = store.get(memory.memory_id)
    history = store.revisions(memory.memory_id)
    result = erasure.run("owner", window["conversation_id"])
    assert result["status"] == "failed" and result["error_code"] == "erasure_storage_error"
    assert store.get(memory.memory_id) == current
    assert store.revisions(memory.memory_id) == history
    assert InteractionStore(db).get(message_id).content == "private original"
    with db.connection() as connection:
        assert not connection.execute("SELECT 1 FROM private_erasure_receipts").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


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
