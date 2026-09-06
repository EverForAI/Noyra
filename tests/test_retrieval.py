from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash
from noyra.mind import MemoryEmbeddingIndex, MemoryStore


class FakeEmbedding:
    name = "test-embedding"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [1.0, 0.0]
            if any(term in text.casefold() for term in ("energy", "renewable", "storage"))
            else [0.0, 1.0]
            for text in texts
        ]


class RetrievalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "noyra.sqlite3")
        self.subject_id = "Noyra-retrieval-test"
        IdentityStore(self.database).ensure(
            self.subject_id, content_hash({"seed": self.subject_id})
        )
        self.events = EventStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_embedding_index_is_rebuildable_and_hybrid_recall_uses_semantics(self) -> None:
        index = MemoryEmbeddingIndex(self.database, FakeEmbedding())
        store = MemoryStore(self.database, embedding_index=index)
        event = self.events.append(self.subject_id, "observation", "test", {"v": 1})
        memory = store.create(
            self.subject_id,
            "semantic",
            "Renewable storage capacity changed.",
            salience=0.4,
            confidence=0.8,
            source_event_ids=(event.event_id,),
        )
        self.assertEqual(index.rebuild_missing(self.subject_id), 1)
        recalls = store.recall(
            self.subject_id,
            "energy outlook",
            context_type="test",
            context_id="semantic",
        )
        self.assertEqual(recalls[0].memory.memory_id, memory.memory_id)
        self.assertGreater(recalls[0].semantic_score, 0)
        self.assertEqual(index.verify_integrity(self.subject_id), 1)


if __name__ == "__main__":
    unittest.main()
