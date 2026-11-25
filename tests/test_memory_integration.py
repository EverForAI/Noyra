from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Self

import pytest

from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.source_provenance import MAX_SOURCE_EVENT_IDS
from noyra.core.types import canonical_json, content_hash, utc_now
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.mind import MemoryIntegrationError, MemoryIntegrationSupervisor, MemoryStore


class MemoryIntegrationTestCase(unittest.TestCase):
    def test_merge_preserves_sources_and_can_rollback(self: Self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-test",
                content_hash({"seed": "integration"}),
            )
            events = EventStore(kernel.database)
            event = events.append(kernel.subject_id, "experience", "test", {"topic": "continuity"})
            other = events.append(kernel.subject_id, "experience", "test", {"topic": "history"})
            review = events.append(kernel.subject_id, "review", "test", {"topic": "synthesis"})
            memories = MemoryStore(kernel.database)
            first = memories.create(
                kernel.subject_id,
                "semantic",
                "Continuity requires state.",
                salience=0.7,
                confidence=0.7,
                source_event_ids=(event.event_id,),
            )
            second = memories.create(
                kernel.subject_id,
                "semantic",
                "Continuity requires history.",
                salience=0.8,
                confidence=0.8,
                source_event_ids=(other.event_id,),
            )
            supervisor = MemoryIntegrationSupervisor(kernel.database, kernel.subject_id)
            integration = supervisor.integrate(
                "merge",
                (first.memory_id, second.memory_id),
                synthesis="Continuity requires state and history.",
                confidence=0.8,
                evidence_event_ids=(review.event_id,),
                reason="evidence synthesis",
            )
            self.assertEqual(memories.get(first.memory_id).status, "superseded")
            assert integration.output_memory_id is not None
            output_id = integration.output_memory_id
            output_sources = (review.event_id, event.event_id, other.event_id)
            self.assertEqual(integration.evidence_event_ids, (review.event_id,))
            self.assertEqual(memories.source_event_ids(output_id), output_sources)
            self.assertEqual(memories.source_event_ids(first.memory_id), (event.event_id,))
            self.assertEqual(memories.source_event_ids(second.memory_id), (other.event_id,))

            second_pass = supervisor.integrate(
                "contradict",
                (output_id, second.memory_id),
                synthesis="The two readings have different scope.",
                confidence=0.7,
                evidence_event_ids=(review.event_id,),
                reason="second pass over previously integrated evidence",
            )
            assert second_pass.output_memory_id is not None
            self.assertEqual(
                memories.source_event_ids(second_pass.output_memory_id), output_sources
            )
            reverted = supervisor.revert(integration.integration_id, reason="new evidence")
            self.assertEqual(reverted.status, "reverted")
            self.assertEqual(memories.get(first.memory_id).status, "active")
            self.assertEqual(memories.get(output_id).status, "archived")
            self.assertEqual(memories.source_event_ids(first.memory_id), (event.event_id,))
            self.assertEqual(memories.source_event_ids(output_id), output_sources)
            counts = memories.verify_lifecycle_integrity(kernel.subject_id)
            self.assertEqual(counts["memory_integrations"], 2)
            self.assertEqual(counts["memory_integration_revisions"], 3)

    def test_combined_sources_at_limit_are_preserved_and_overflow_is_atomic(self: Self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-limit",
                content_hash("integration limit"),
            )
            try:
                subject = kernel.subject_id
                events = EventStore(kernel.database)
                ids = tuple(
                    events.append(subject, "experience", "test", {"number": index}).event_id
                    for index in range(MAX_SOURCE_EVENT_IDS + 1)
                )
                memories = MemoryStore(kernel.database)
                first = memories.create(
                    subject,
                    "semantic",
                    "First",
                    salience=0.5,
                    confidence=0.5,
                    source_event_ids=ids[:-1],
                )
                second = memories.create(
                    subject,
                    "semantic",
                    "Second",
                    salience=0.5,
                    confidence=0.5,
                    source_event_ids=(ids[0],),
                )
                supervisor = MemoryIntegrationSupervisor(kernel.database, subject)
                with pytest.raises(MemoryIntegrationError, match="exceeds the memory source limit"):
                    supervisor.integrate(
                        "merge",
                        (first.memory_id, second.memory_id),
                        synthesis="Merged",
                        confidence=0.5,
                        evidence_event_ids=(ids[-1],),
                        reason="new evidence",
                    )
                self.assertEqual(memories.get(first.memory_id), first)
                self.assertEqual(memories.get(second.memory_id), second)
                with kernel.database.connection() as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2
                    )
                    self.assertIsNone(
                        connection.execute("SELECT 1 FROM memory_integrations").fetchone()
                    )
                result = supervisor.integrate(
                    "merge",
                    (first.memory_id, second.memory_id),
                    synthesis="Merged",
                    confidence=0.5,
                    evidence_event_ids=(ids[0],),
                    reason="overlapping evidence",
                )
                assert result.output_memory_id is not None
                self.assertEqual(memories.source_event_ids(result.output_memory_id), ids[:-1])
            finally:
                kernel.close()

    def test_withdrawn_source_blocks_integration_without_partial_writes(self: Self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-withdrawal",
                content_hash("integration withdrawal"),
            )
            try:
                subject = kernel.subject_id
                conversation = ConversationStore(kernel.database).create(subject, "Private")
                message = InteractionStore(kernel.database).receive(
                    subject,
                    "web",
                    "web-user",
                    "A private suggestion",
                    conversation_id=conversation["conversation_id"],
                )
                with kernel.database.connection() as connection:
                    event_id = str(
                        connection.execute(
                            "SELECT event_id FROM events WHERE subject_id=? "
                            "AND event_type='interaction_received' "
                            "AND json_extract(payload_json,'$.interaction_id')=?",
                            (subject, message.interaction_id),
                        ).fetchone()[0]
                    )
                memories = MemoryStore(kernel.database)
                first = memories.create(
                    subject,
                    "episodic",
                    "Private source one",
                    salience=0.5,
                    confidence=0.5,
                    source_event_ids=(event_id,),
                )
                second = memories.create(
                    subject,
                    "episodic",
                    "Private source two",
                    salience=0.5,
                    confidence=0.5,
                    source_event_ids=(event_id,),
                )
                with kernel.database.transaction() as connection:
                    connection.execute(
                        "UPDATE private_conversations SET status='deleting' "
                        "WHERE conversation_id=?",
                        (conversation["conversation_id"],),
                    )
                supervisor = MemoryIntegrationSupervisor(kernel.database, subject)
                with pytest.raises(MemoryIntegrationError, match="source events are unavailable"):
                    supervisor.integrate(
                        "merge",
                        (first.memory_id, second.memory_id),
                        synthesis="Must not commit",
                        confidence=0.5,
                        evidence_event_ids=(event_id,),
                        reason="withdrawal test",
                    )
                self.assertEqual(memories.get(first.memory_id), first)
                self.assertEqual(memories.get(second.memory_id), second)
                with kernel.database.connection() as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2
                    )
                    self.assertIsNone(
                        connection.execute("SELECT 1 FROM memory_integrations").fetchone()
                    )
            finally:
                kernel.close()

    def test_lifecycle_integrity_rejects_integration_storage_and_ownership_damage(
        self: Self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-integrity",
                content_hash({"seed": "integration-integrity"}),
            )
            event = EventStore(kernel.database).append(
                kernel.subject_id, "experience", "test", {"topic": "integrity"}
            )
            memories = MemoryStore(kernel.database)
            first = memories.create(
                kernel.subject_id,
                "semantic",
                "Integrity source one.",
                salience=0.7,
                confidence=0.7,
                source_event_ids=(event.event_id,),
            )
            second = memories.create(
                kernel.subject_id,
                "semantic",
                "Integrity source two.",
                salience=0.8,
                confidence=0.8,
                source_event_ids=(event.event_id,),
            )
            supervisor = MemoryIntegrationSupervisor(kernel.database, kernel.subject_id)
            integration = supervisor.integrate(
                "merge",
                (first.memory_id, second.memory_id),
                synthesis="Integrity sources merged.",
                confidence=0.8,
                evidence_event_ids=(event.event_id,),
                reason="integrity fixture",
            )
            with kernel.database.connection() as connection:
                original = dict(
                    connection.execute(
                        "SELECT * FROM memory_integrations WHERE integration_id = ?",
                        (integration.integration_id,),
                    ).fetchone()
                )

            with kernel.database.transaction() as connection:
                trigger = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='prevent_memory_integration_update'"
                ).fetchone()[0]
                connection.execute("DROP TRIGGER prevent_memory_integration_update")
                connection.execute(
                    "UPDATE memory_integrations SET source_memory_ids_json = ? "
                    "WHERE integration_id = ?",
                    (
                        original["source_memory_ids_json"].encode("utf-8"),
                        integration.integration_id,
                    ),
                )
                connection.execute(trigger)
            with self.assertRaises(IntegrityError):
                memories.verify_lifecycle_integrity(kernel.subject_id)

            missing_evidence = ("evt_missing",)
            missing_hash = supervisor._hash(
                integration.operation,
                integration.source_memory_ids,
                integration.output_memory_id,
                missing_evidence,
                integration.reason,
                integration.confidence,
                integration.status,
            )
            with kernel.database.transaction() as connection:
                trigger = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='prevent_memory_integration_update'"
                ).fetchone()[0]
                connection.execute("DROP TRIGGER prevent_memory_integration_update")
                connection.execute(
                    "UPDATE memory_integrations SET source_memory_ids_json = ?, "
                    "evidence_event_ids_json = ?, state_hash = ? WHERE integration_id = ?",
                    (
                        original["source_memory_ids_json"],
                        canonical_json(list(missing_evidence)),
                        missing_hash,
                        integration.integration_id,
                    ),
                )
                connection.execute(trigger)
            with self.assertRaises(IntegrityError):
                memories.verify_lifecycle_integrity(kernel.subject_id)

    def test_lifecycle_integrity_rejects_integration_revision_state_mismatch(self: Self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-revisions",
                content_hash({"seed": "integration-revisions"}),
            )
            event = EventStore(kernel.database).append(
                kernel.subject_id, "experience", "test", {"topic": "revisions"}
            )
            memories = MemoryStore(kernel.database)
            source_ids = tuple(
                memories.create(
                    kernel.subject_id,
                    "semantic",
                    f"Revision source {index}.",
                    salience=0.7,
                    confidence=0.7,
                    source_event_ids=(event.event_id,),
                ).memory_id
                for index in range(2)
            )
            integration = MemoryIntegrationSupervisor(kernel.database, kernel.subject_id).integrate(
                "merge",
                source_ids,
                synthesis="Revision sources merged.",
                confidence=0.7,
                evidence_event_ids=(event.event_id,),
                reason="revision fixture",
            )
            now = utc_now()
            payload = {
                "integration_id": integration.integration_id,
                "action": "revert",
                "reason": "forged revert",
                "created_at": now,
            }
            with kernel.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO memory_integration_revisions(revision_id, integration_id, "
                    "action, reason, state_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "memintrev_forged",
                        integration.integration_id,
                        "revert",
                        "forged revert",
                        content_hash(payload),
                        now,
                    ),
                )
            with self.assertRaises(IntegrityError):
                memories.verify_lifecycle_integrity(kernel.subject_id)
