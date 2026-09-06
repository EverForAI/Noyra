from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, utc_now
from noyra.mind import MemoryIntegrationSupervisor, MemoryStore


class MemoryIntegrationTestCase(unittest.TestCase):
    def test_merge_preserves_sources_and_can_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-integration-test",
                content_hash({"seed": "integration"}),
            )
            event = EventStore(kernel.database).append(
                kernel.subject_id, "experience", "test", {"topic": "continuity"}
            )
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
                source_event_ids=(event.event_id,),
            )
            supervisor = MemoryIntegrationSupervisor(kernel.database, kernel.subject_id)
            integration = supervisor.integrate(
                "merge",
                (first.memory_id, second.memory_id),
                synthesis="Continuity requires state and history.",
                confidence=0.8,
                evidence_event_ids=(event.event_id,),
                reason="evidence synthesis",
            )
            self.assertEqual(memories.get(first.memory_id).status, "superseded")
            self.assertIsNotNone(integration.output_memory_id)
            reverted = supervisor.revert(integration.integration_id, reason="new evidence")
            self.assertEqual(reverted.status, "reverted")
            self.assertEqual(memories.get(first.memory_id).status, "active")
            assert integration.output_memory_id is not None
            self.assertEqual(memories.get(integration.output_memory_id).status, "archived")
            counts = memories.verify_lifecycle_integrity(kernel.subject_id)
            self.assertEqual(counts["memory_integrations"], 1)
            self.assertEqual(counts["memory_integration_revisions"], 2)

    def test_lifecycle_integrity_rejects_integration_storage_and_ownership_damage(
        self,
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
                connection.execute(
                    "UPDATE memory_integrations SET source_memory_ids_json = ? "
                    "WHERE integration_id = ?",
                    (
                        original["source_memory_ids_json"].encode("utf-8"),
                        integration.integration_id,
                    ),
                )
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
            with self.assertRaises(IntegrityError):
                memories.verify_lifecycle_integrity(kernel.subject_id)

    def test_lifecycle_integrity_rejects_integration_revision_state_mismatch(self) -> None:
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
