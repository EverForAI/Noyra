from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash
from noyra.mind import EntityStore


class EntityStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "noyra.sqlite3")
        self.subject_id = "Noyra-entity-test"
        IdentityStore(self.database).ensure(
            self.subject_id, content_hash({"seed": self.subject_id})
        )
        self.event = EventStore(self.database).append(
            self.subject_id, "observation", "test", {"value": 1}
        )
        self.store = EntityStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_temporal_relation_keeps_sources_and_supports_as_of(self) -> None:
        source = self.store.upsert(self.subject_id, "project", "noyra", "Noyra")
        target = self.store.upsert(self.subject_id, "concept", "continuity", "Continuity")
        relation = self.store.relation(
            self.subject_id,
            source.entity_id,
            "explores",
            target.entity_id,
            source_event_ids=(self.event.event_id,),
            confidence=0.8,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_until="2027-01-01T00:00:00+00:00",
        )
        found = self.store.relations(
            self.subject_id, entity_id=source.entity_id, as_of="2026-08-01T00:00:00+00:00"
        )
        self.assertEqual(found[0].relation_id, relation.relation_id)
        self.assertEqual(found[0].source_event_ids, (self.event.event_id,))
        self.assertEqual(
            self.store.verify_integrity(self.subject_id),
            {"entities": 2, "relations": 1, "links": 0},
        )

    def test_entity_links_memory_belief_or_event_without_copying_source(self) -> None:
        entity = self.store.upsert(self.subject_id, "concept", "continuity", "Continuity")
        link = self.store.link_evidence(
            self.subject_id,
            entity.entity_id,
            "event",
            self.event.event_id,
            role="observed_in",
            confidence=0.8,
        )
        self.assertEqual(self.store.evidence_links(self.subject_id, entity.entity_id), [link])

    def test_cross_subject_relation_is_rejected(self) -> None:
        other = "Noyra-other-entity"
        IdentityStore(self.database).ensure(other, content_hash({"seed": other}))
        first = self.store.upsert(self.subject_id, "person", "alice", "Alice")
        second = self.store.upsert(other, "person", "alice", "Alice")
        with self.assertRaises(ValueError):
            self.store.relation(
                self.subject_id,
                first.entity_id,
                "knows",
                second.entity_id,
                source_event_ids=(self.event.event_id,),
                confidence=0.5,
            )


if __name__ == "__main__":
    unittest.main()
