from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.mind import MemoryConsolidator, MemoryStore
from noyra.mind.types import MemoryRecord, MemoryType


class MemoryLifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "noyra.sqlite3")
        self.subject_id = "Noyra-memory-lifecycle"
        IdentityStore(self.database).ensure(
            self.subject_id, content_hash({"seed": "memory-lifecycle"})
        )
        self.clock_value = "2026-08-12T00:00:00.000+00:00"
        self.events = EventStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_memory(
        self,
        content: str,
        *,
        memory_type: MemoryType = "semantic",
        salience: float = 0.5,
        confidence: float = 0.7,
        occurred_at: str | None = None,
    ) -> MemoryRecord:
        event = self.events.append(
            self.subject_id,
            "memory-evidence",
            "test",
            {"content_hash": content_hash(content)},
            occurred_at=occurred_at or self.clock_value,
        )
        store = MemoryStore(self.database, clock=lambda: occurred_at or self.clock_value)
        return store.create(
            self.subject_id,
            memory_type,
            content,
            salience=salience,
            confidence=confidence,
            source_event_ids=(event.event_id,),
        )

    def test_contextual_recall_ranks_matches_and_records_access(self) -> None:
        store = MemoryStore(self.database, clock=lambda: self.clock_value)
        related = self.create_memory(
            "Renewable storage evidence changed the energy forecast.", salience=0.55
        )
        self.create_memory("A conversation about poetry and color.", salience=0.9)
        recalls = store.recall(
            self.subject_id,
            "energy storage forecast",
            context_type="test",
            context_id="context-1",
            limit=4,
        )
        self.assertEqual(recalls[0].memory.memory_id, related.memory_id)
        self.assertGreater(recalls[0].lexical_score, 0)
        store.recall(
            self.subject_id,
            "energy storage forecast",
            context_type="test",
            context_id="context-1",
            limit=4,
        )
        history = store.access_history(related.memory_id)
        self.assertEqual(history[0]["access_count"], 2)
        self.assertEqual(len(history[0]["query_hash"]), 64)

    def test_repeated_recall_strengthens_memory_during_consolidation(self) -> None:
        store = MemoryStore(self.database, clock=lambda: self.clock_value)
        memory = self.create_memory("A repeatedly useful procedural clue.", salience=0.4)
        for index in range(3):
            store.recall(
                self.subject_id,
                "procedural clue useful",
                context_type="test",
                context_id=f"context-{index}",
                limit=1,
            )
        later = "2026-08-13T00:00:00.000+00:00"
        consolidator = MemoryConsolidator(
            self.database,
            self.subject_id,
            clock=lambda: later,
            interval_seconds=0,
            minimum_active_memories=1,
        )
        self.assertEqual(consolidator.run_due(), "memory_consolidation_committed")
        revised = MemoryStore(self.database).get(memory.memory_id)
        self.assertGreater(revised.salience, 0.4)
        self.assertEqual(revised.status, "active")
        latest = consolidator.latest()
        assert latest is not None
        self.assertEqual(latest.strengthened_memory_ids, (memory.memory_id,))

    def test_duplicate_and_stale_weak_memory_are_archived_without_deletion(self) -> None:
        old = (datetime.fromisoformat(self.clock_value) - timedelta(days=365)).isoformat(
            timespec="milliseconds"
        )
        first = self.create_memory("The same exact episode.", memory_type="episodic")
        duplicate = self.create_memory("The same exact episode.", memory_type="episodic")
        stale = self.create_memory(
            "A weak old detail.",
            memory_type="episodic",
            salience=0.1,
            confidence=0.3,
            occurred_at=old,
        )
        self.create_memory("A retained anchor memory.", salience=0.8)
        consolidator = MemoryConsolidator(
            self.database,
            self.subject_id,
            clock=lambda: self.clock_value,
            interval_seconds=0,
            archive_after_days=180,
            minimum_active_memories=1,
        )
        self.assertEqual(consolidator.run_due(), "memory_consolidation_committed")
        statuses = {
            item.memory_id: item.status
            for item in (
                MemoryStore(self.database).get(first.memory_id),
                MemoryStore(self.database).get(duplicate.memory_id),
                MemoryStore(self.database).get(stale.memory_id),
            )
        }
        self.assertEqual(list(statuses.values()).count("archived"), 2)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE subject_id = ?", (self.subject_id,)
                ).fetchone()[0],
                4,
            )
        archived = MemoryStore(self.database).search(self.subject_id, query="weak old")
        self.assertEqual(archived, [])

    def test_memory_lifecycle_integrity_detects_access_tampering(self) -> None:
        store = MemoryStore(self.database, clock=lambda: self.clock_value)
        memory = self.create_memory("Integrity relevant memory.")
        store.recall(
            self.subject_id,
            "integrity relevant",
            context_type="test",
            context_id="integrity",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE memory_accesses SET access_count = 99 WHERE memory_id = ?",
                (memory.memory_id,),
            )
        with self.assertRaises(IntegrityError):
            store.verify_lifecycle_integrity(self.subject_id)

    def test_memory_lifecycle_integrity_rejects_corrupt_access_numbers(self) -> None:
        store = MemoryStore(self.database, clock=lambda: self.clock_value)
        memory = self.create_memory("Persisted access parser fixture.")
        store.recall(
            self.subject_id,
            "persisted access parser",
            context_type="test",
            context_id="numeric-corruption",
        )
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM memory_accesses WHERE memory_id = ?", (memory.memory_id,)
                ).fetchone()
            )
        for column, value in (("relevance", "nan"), ("access_count", 1.5)):
            with self.subTest(column=column):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE memory_accesses SET "{column}" = ? WHERE memory_id = ?',
                        (value, memory.memory_id),
                    )
                with self.assertRaises(IntegrityError):
                    store.verify_lifecycle_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE memory_accesses SET "{column}" = ? WHERE memory_id = ?',
                        (original[column], memory.memory_id),
                    )

    def test_memory_lifecycle_integrity_rejects_corrupt_consolidation_fields(self) -> None:
        self.create_memory("Consolidation parser fixture.")
        consolidator = MemoryConsolidator(
            self.database,
            self.subject_id,
            clock=lambda: self.clock_value,
            interval_seconds=0,
        )
        consolidator.run_due()
        store = MemoryStore(self.database)
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_memory_consolidation_update")
            row = dict(
                connection.execute(
                    "SELECT * FROM memory_consolidation_runs WHERE subject_id = ?",
                    (self.subject_id,),
                ).fetchone()
            )
        cases = (
            ("archived_memory_ids_json", "{"),
            ("status", "invalid"),
            ("reviewed_count", 1.5),
        )
        for column, value in cases:
            with self.subTest(column=column):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE memory_consolidation_runs SET "{column}" = ? '
                        "WHERE consolidation_id = ?",
                        (value, row["consolidation_id"]),
                    )
                with self.assertRaises(IntegrityError):
                    store.verify_lifecycle_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE memory_consolidation_runs SET "{column}" = ? '
                        "WHERE consolidation_id = ?",
                        (row[column], row["consolidation_id"]),
                    )

    def test_schema_version_sixteen_and_append_only_consolidation(self) -> None:
        with self.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.create_memory("One consolidation fixture.")
        consolidator = MemoryConsolidator(
            self.database,
            self.subject_id,
            clock=lambda: self.clock_value,
            interval_seconds=0,
        )
        consolidator.run_due()
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM memory_consolidation_runs")


if __name__ == "__main__":
    unittest.main()
