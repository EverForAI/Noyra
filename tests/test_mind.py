from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.mind import (
    AffectImpulse,
    AppraisalInput,
    BeliefStore,
    CausalStore,
    EntityStore,
    GoalCandidate,
    GoalStore,
    MemoryBlockStore,
    MemoryEmbeddingIndex,
    MemoryStore,
    MindEngine,
    RelationshipStore,
)
from noyra.mind.errors import CausalValidationError, MindStateConflictError


class FakeEmbedding:
    name = "test-embedding"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class MindTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "noyra.sqlite3")
        self.subject_id = "Noyra-mind-test"
        IdentityStore(self.database).ensure(self.subject_id, content_hash({"seed": "mind-test"}))
        self.events = EventStore(self.database)
        self.event = self.events.append(
            self.subject_id,
            "world_observation",
            "test",
            {"observation": "A new pattern appeared."},
        )
        self.clock_value = "2026-08-11T00:00:00.000+00:00"
        self.engine = MindEngine(self.database, clock=lambda: self.clock_value)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def appraisal(
        *, novelty: float = 0.8, congruence: float = 0.2, controllability: float = 0.7
    ) -> AppraisalInput:
        return AppraisalInput(
            novelty=novelty,
            goal_congruence=congruence,
            controllability=controllability,
            certainty=0.75,
            agency="environment",
            narrative="The event may matter to current understanding.",
        )

    @staticmethod
    def impulse(
        emotion: str = "curiosity",
        *,
        amount: float = 0.7,
        target_type: str = "world",
        target_id: str | None = None,
        goal_effect: float = 0.0,
        decay_rate: float = 0.1,
    ) -> AffectImpulse:
        return AffectImpulse(
            emotion_type=emotion,
            target_type=target_type,
            target_id=target_id,
            impulse=amount,
            valence=0.4 if emotion == "curiosity" else -0.6,
            arousal=0.7,
            dominance=0.1,
            decay_rate=decay_rate,
            goal_effect=goal_effect,
        )

    @staticmethod
    def candidate(
        title: str = "Investigate the pattern",
        *,
        origin: Literal["self", "environment", "human_proposal", "maintenance", "mixed"] = "self",
    ) -> GoalCandidate:
        return GoalCandidate(
            title=title,
            description="Gather evidence and test whether the pattern persists.",
            origin=origin,
            priority=0.7,
            commitment=0.6,
            motive_emotion="curiosity",
            motive_target_type="world",
            minimum_motive_intensity=0.5,
        )

    def new_event(self, label: str) -> str:
        return self.events.append(self.subject_id, "test_event", "test", {"label": label}).event_id

    def test_current_schema_and_mind_append_only_triggers_exist(self) -> None:
        with self.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            triggers = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertIn("prevent_memory_delete", triggers)
        self.assertIn("prevent_causal_link_update", triggers)
        self.assertIn("prevent_psychological_snapshot_delete", triggers)

    def test_schema_migrates_from_version_two(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-v2.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
        connection.commit()
        connection.close()
        migrated = Database(legacy_path)
        with migrated.connection() as migrated_connection:
            version = migrated_connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            mind_table = migrated_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'appraisals'"
            ).fetchone()
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertIsNotNone(mind_table)

    def test_memory_is_revisioned_searchable_and_not_deletable(self) -> None:
        store = MemoryStore(self.database)
        memory = store.create(
            self.subject_id,
            "episodic",
            "I observed a persistent pattern.",
            salience=0.8,
            confidence=0.6,
            source_event_ids=(self.event.event_id,),
        )
        revised = store.revise(
            memory.memory_id,
            content="I observed a persistent pattern twice.",
            salience=0.9,
            confidence=0.75,
            status="active",
            reason="new confirming evidence",
            source_event_ids=(self.event.event_id,),
            expected_revision=1,
        )
        self.assertEqual(revised.current_revision, 2)
        self.assertEqual(len(store.revisions(memory.memory_id)), 2)
        self.assertEqual(
            store.search(self.subject_id, query="twice")[0].memory_id, memory.memory_id
        )
        with self.assertRaises(MindStateConflictError):
            store.revise(
                memory.memory_id,
                content=revised.content,
                salience=0.9,
                confidence=0.75,
                status="active",
                reason="stale writer",
                source_event_ids=(self.event.event_id,),
                expected_revision=1,
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM memories WHERE memory_id = ?", (memory.memory_id,))

    def test_memory_hash_tampering_is_detected(self) -> None:
        store = MemoryStore(self.database)
        memory = store.create(
            self.subject_id,
            "semantic",
            "A provisional fact.",
            salience=0.5,
            confidence=0.5,
            source_event_ids=(self.event.event_id,),
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE memories SET content = 'tampered' WHERE memory_id = ?",
                (memory.memory_id,),
            )
        with self.assertRaises(IntegrityError):
            store.get(memory.memory_id)

    def test_entity_integrity_classifies_persisted_corruption(self) -> None:
        store = EntityStore(self.database)
        source = store.upsert(
            self.subject_id,
            "person",
            "source",
            "Source",
            aliases=("S",),
            confidence=0.8,
        )
        target = store.upsert(self.subject_id, "person", "target", "Target", confidence=0.7)
        relation = store.relation(
            self.subject_id,
            source.entity_id,
            "knows",
            target.entity_id,
            source_event_ids=(self.event.event_id,),
            confidence=0.9,
        )
        link = store.link_evidence(
            self.subject_id,
            source.entity_id,
            "event",
            self.event.event_id,
            role="observed",
            confidence=0.6,
        )
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_entity_relation_update")
            connection.execute("DROP TRIGGER prevent_entity_evidence_link_update")

        cases = (
            ("entities", "entity_id", source.entity_id, "aliases_json", "{"),
            ("entities", "entity_id", source.entity_id, "confidence", "nan"),
            (
                "entity_relations",
                "relation_id",
                relation.relation_id,
                "source_event_ids_json",
                "{",
            ),
            ("entity_relations", "relation_id", relation.relation_id, "confidence", "nan"),
            ("entity_relations", "relation_id", relation.relation_id, "status", "invalid"),
            ("entity_evidence_links", "link_id", link.link_id, "confidence", "nan"),
        )
        for table, key, identifier, column, value in cases:
            with self.subTest(table=table, column=column):
                with self.database.transaction() as connection:
                    original = connection.execute(
                        f'SELECT "{column}" FROM "{table}" WHERE "{key}" = ?',
                        (identifier,),
                    ).fetchone()[0]
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key}" = ?',
                        (value, identifier),
                    )
                with self.assertRaises(IntegrityError):
                    store.verify_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key}" = ?',
                        (original, identifier),
                    )

    def test_memory_embedding_integrity_classifies_persisted_corruption(self) -> None:
        index = MemoryEmbeddingIndex(self.database, FakeEmbedding())
        memory = MemoryStore(self.database, embedding_index=index).create(
            self.subject_id,
            "semantic",
            "Embedding integrity fixture.",
            salience=0.5,
            confidence=0.8,
            source_event_ids=(self.event.event_id,),
        )
        index.index_memory(self.subject_id, memory.memory_id, memory.content)
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,)
                ).fetchone()
            )
        for column, value in (
            ("vector_json", "{"),
            ("vector_json", "[NaN, 0.0]"),
            (
                "vector_json",
                sqlite3.Binary(str(original["vector_json"]).encode("utf-8")),
            ),
            ("dimensions", 1.5),
            (
                "dimensions",
                sqlite3.Binary(str(original["dimensions"]).encode("ascii")),
            ),
        ):
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE memory_embeddings SET "{column}" = ? WHERE memory_id = ?',
                        (value, memory.memory_id),
                    )
                with self.assertRaises(IntegrityError):
                    index.verify_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE memory_embeddings SET "{column}" = ? WHERE memory_id = ?',
                        (original[column], memory.memory_id),
                    )

    def test_memory_block_integrity_rejects_fractional_versions(self) -> None:
        store = MemoryBlockStore(self.database)
        block = store.create(
            self.subject_id,
            "working_context",
            "current",
            "Current task context.",
            reason="test fixture",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE memory_blocks SET version = 1.5 WHERE block_id = ?",
                (block.block_id,),
            )
        with self.assertRaises(IntegrityError):
            store.verify_integrity(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE memory_blocks SET version = 1 WHERE block_id = ?", (block.block_id,)
            )
            connection.execute("DROP TRIGGER prevent_memory_block_revision_update")
            connection.execute(
                "UPDATE memory_block_revisions SET version = 1.5 WHERE block_id = ?",
                (block.block_id,),
            )
        with self.assertRaises(IntegrityError):
            store.verify_integrity(self.subject_id)

    def test_belief_requires_evidence_and_keeps_revisions(self) -> None:
        store = BeliefStore(self.database)
        belief = store.create(
            self.subject_id,
            "The pattern is persistent.",
            confidence=0.55,
            scope="observed system",
            supporting_event_ids=(self.event.event_id,),
        )
        revised = store.revise(
            belief.belief_id,
            proposition="The pattern may be persistent under current conditions.",
            confidence=0.4,
            status="qualified",
            supporting_event_ids=(self.event.event_id,),
            counter_event_ids=(self.event.event_id,),
            reason="counterevidence narrowed the claim",
            expected_revision=1,
        )
        self.assertEqual(revised.status, "qualified")
        self.assertEqual(revised.current_revision, 2)
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE belief_revisions SET reason = 'rewrite' WHERE belief_id = ?",
                (belief.belief_id,),
            )

    def test_relationship_state_is_revisioned_and_integrity_checked(self) -> None:
        store = RelationshipStore(self.database)
        relationship = store.ensure(
            self.subject_id,
            "human",
            "human-1",
            "Research collaborator",
            source_event_ids=(self.event.event_id,),
        )
        revised = store.revise(
            relationship.relationship_id,
            trust=0.35,
            affinity=0.2,
            conflict=0.1,
            familiarity=0.4,
            boundaries={"private_memory": "not_shared"},
            reason="interaction provided evidence",
            source_event_ids=(self.event.event_id,),
            expected_revision=1,
        )
        self.assertEqual(revised.trust, 0.35)
        self.assertEqual(revised.boundaries["private_memory"], "not_shared")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE relationships SET trust = 0.9 WHERE relationship_id = ?",
                (relationship.relationship_id,),
            )
        with self.assertRaises(IntegrityError):
            store.get(relationship.relationship_id)

    def test_any_emotion_label_has_causal_state_and_psychological_snapshot(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(congruence=-0.7),
            [self.impulse("anger", amount=0.65)],
        )
        affect = self.engine.current_affect(self.subject_id)[0]
        self.assertEqual(affect.emotion_type, "anger")
        self.assertAlmostEqual(affect.intensity, 0.65)
        self.assertLess(result.mood.valence, 0)
        self.assertEqual(self.engine.latest_snapshot(self.subject_id).version, 1)
        links = CausalStore(self.database).links_from(self.subject_id, "event", self.event.event_id)
        self.assertEqual(links[0].target_type, "appraisal")

    def test_curiosity_can_generate_autonomous_goal_candidate(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
        )
        goal = result.goals[0]
        self.assertEqual(goal.origin, "self")
        self.assertEqual(goal.status, "candidate")
        self.assertGreater(goal.selection_score, 0)
        report = self.engine.verify_integrity(self.subject_id)
        self.assertEqual(report["goals"], 1)
        self.assertEqual(report["psychological_snapshots"], 1)
        links = CausalStore(self.database).links_from(
            self.subject_id, "affect_transition", result.transitions[-1].transition_id
        )
        self.assertTrue(any(link.relation == "motivated" for link in links))

    def test_event_processing_is_idempotent_and_conflict_checked(self) -> None:
        first = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
            idempotency_key="primary-observation",
        )
        duplicate = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
            idempotency_key="primary-observation",
        )
        self.assertEqual(first.appraisal.appraisal_id, duplicate.appraisal.appraisal_id)
        self.assertEqual(first.snapshot.snapshot_id, duplicate.snapshot.snapshot_id)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM appraisals").fetchone()[0],
                1,
            )
        with self.assertRaises(MindStateConflictError):
            self.engine.process_event(
                self.subject_id,
                self.event.event_id,
                self.appraisal(congruence=-0.5),
                [self.impulse()],
                idempotency_key="primary-observation",
            )

    def test_unsupported_autonomous_goal_rolls_back_entire_experience(self) -> None:
        unsupported = self.candidate("Uncaused goal").model_copy(update={"motive_emotion": "hope"})
        with self.assertRaises(CausalValidationError):
            self.engine.process_event(
                self.subject_id,
                self.event.event_id,
                self.appraisal(),
                [self.impulse("curiosity")],
                [unsupported],
            )
        with self.database.connection() as connection:
            appraisals = connection.execute("SELECT COUNT(*) FROM appraisals").fetchone()[0]
            affects = connection.execute("SELECT COUNT(*) FROM affect_components").fetchone()[0]
        self.assertEqual(appraisals, 0)
        self.assertEqual(affects, 0)

    def test_human_proposal_is_not_active_until_subject_accepts_it(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [],
            [self.candidate("Consider a human proposal", origin="human_proposal")],
        )
        store = GoalStore(self.database)
        goal = result.goals[0]
        self.assertEqual(goal.status, "proposed")
        with self.assertRaises(PermissionError):
            store.accept_human_proposal(
                goal.goal_id,
                rationale="human command",
                causal_source_ids=(result.appraisal.appraisal_id,),
                actor="human",
            )
        accepted = store.accept_human_proposal(
            goal.goal_id,
            rationale="subject independently accepts exploration",
            causal_source_ids=(result.appraisal.appraisal_id,),
        )
        active = store.activate(
            goal.goal_id,
            rationale="subject commits resources",
            causal_source_ids=(result.appraisal.appraisal_id,),
        )
        self.assertEqual(accepted.status, "candidate")
        self.assertEqual(active.status, "active")

    def test_negative_affect_can_force_active_goal_reconsideration(self) -> None:
        formation = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
        )
        goal = GoalStore(self.database).activate(
            formation.goals[0].goal_id,
            rationale="begin investigation",
            causal_source_ids=(formation.appraisal.appraisal_id,),
        )
        for index in range(2):
            event_id = self.new_event(f"setback-{index}")
            self.engine.process_event(
                self.subject_id,
                event_id,
                self.appraisal(congruence=-0.9, controllability=0.2),
                [
                    self.impulse(
                        "frustration",
                        amount=0.8,
                        target_type="goal",
                        target_id=goal.goal_id,
                        goal_effect=-1.0,
                    )
                ],
            )
        revised = GoalStore(self.database).get(goal.goal_id)
        self.assertEqual(revised.status, "reconsidering")
        self.assertLessEqual(revised.emotional_pressure, -0.7)

    def test_affect_decays_with_elapsed_time_and_records_transition(self) -> None:
        self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse(amount=1.0, decay_rate=0.5)],
        )
        self.clock_value = "2026-08-11T01:00:00.000+00:00"
        result = self.engine.process_event(
            self.subject_id,
            self.new_event("one-hour-later"),
            self.appraisal(novelty=0.1),
            [],
        )
        affect = self.engine.current_affect(self.subject_id)[0]
        self.assertAlmostEqual(affect.intensity, 0.5)
        self.assertTrue(any(item.impulse == 0 for item in result.transitions))

    def test_goal_pressure_recedes_as_its_causal_emotion_decays(self) -> None:
        formation = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
        )
        store = GoalStore(self.database)
        goal = store.activate(
            formation.goals[0].goal_id,
            rationale="start work",
            causal_source_ids=(formation.appraisal.appraisal_id,),
        )
        self.engine.process_event(
            self.subject_id,
            self.new_event("setback"),
            self.appraisal(congruence=-0.8),
            [
                self.impulse(
                    "frustration",
                    amount=0.8,
                    target_type="goal",
                    target_id=goal.goal_id,
                    goal_effect=-1.0,
                    decay_rate=0.5,
                )
            ],
        )
        self.assertAlmostEqual(store.get(goal.goal_id).emotional_pressure, -0.8)
        self.clock_value = "2026-08-11T01:00:00.000+00:00"
        self.engine.process_event(
            self.subject_id,
            self.new_event("recovery-time"),
            self.appraisal(novelty=0.1),
            [],
        )
        self.assertAlmostEqual(store.get(goal.goal_id).emotional_pressure, -0.4)

    def test_cross_subject_event_cannot_change_mind_state(self) -> None:
        other_subject = "Noyra-other-mind"
        IdentityStore(self.database).ensure(other_subject, content_hash({"seed": "other"}))
        other_event = self.events.append(other_subject, "test", "test", {}).event_id
        with self.assertRaises(CausalValidationError):
            self.engine.process_event(
                self.subject_id,
                other_event,
                self.appraisal(),
                [self.impulse()],
            )

    def test_snapshot_and_goal_hash_tampering_are_detected(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
            [self.candidate()],
        )
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_psychological_snapshot_update")
            connection.execute(
                "UPDATE psychological_snapshots SET state_json = '{}' WHERE snapshot_id = ?",
                (result.snapshot.snapshot_id,),
            )
        with self.assertRaises(IntegrityError):
            self.engine.latest_snapshot(self.subject_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE goals SET priority = 0.1 WHERE goal_id = ?",
                (result.goals[0].goal_id,),
            )
        with self.assertRaises(IntegrityError):
            GoalStore(self.database).get(result.goals[0].goal_id)

    def test_integrity_audit_detects_current_revision_divergence(self) -> None:
        memory = MemoryStore(self.database).create(
            self.subject_id,
            "episodic",
            "Revision anchor.",
            salience=0.5,
            confidence=0.7,
            source_event_ids=(self.event.event_id,),
        )
        self.assertEqual(self.engine.verify_integrity(self.subject_id)["memories"], 1)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE memories SET current_revision = 2 WHERE memory_id = ?",
                (memory.memory_id,),
            )
        with self.assertRaises(IntegrityError):
            self.engine.verify_integrity(self.subject_id)

    def test_integrity_audit_classifies_malformed_persisted_numbers_as_corruption(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
        )
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM mood_states WHERE subject_id = ?", (self.subject_id,)
                ).fetchone()
            )
        cases = (
            ("version", "bad", original["state_hash"]),
            ("version", 1.5, original["state_hash"]),
            (
                "version",
                sqlite3.Binary(str(original["version"]).encode("ascii")),
                original["state_hash"],
            ),
            (
                "valence",
                sqlite3.Binary(str(original["valence"]).encode("ascii")),
                original["state_hash"],
            ),
            (
                "valence",
                2.0,
                MindEngine._mood_hash(
                    2.0,
                    float(original["arousal"]),
                    float(original["stability"]),
                    int(original["version"]),
                ),
            ),
        )
        for column, value, corrupted_hash in cases:
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE mood_states SET "{column}" = ?, state_hash = ? '
                        "WHERE subject_id = ?",
                        (value, corrupted_hash, self.subject_id),
                    )
                with self.assertRaises(IntegrityError):
                    self.engine.verify_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE mood_states SET "{column}" = ?, state_hash = ? '
                        "WHERE subject_id = ?",
                        (original[column], original["state_hash"], self.subject_id),
                    )
        self.assertEqual(result.mood.version, original["version"])

    def test_integrity_audit_rejects_blob_numbers_across_mind_rows(self) -> None:
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
        )
        memory = MemoryStore(self.database).create(
            self.subject_id,
            "episodic",
            "Blob parser memory fixture.",
            salience=0.6,
            confidence=0.7,
            source_event_ids=(self.event.event_id,),
        )
        belief = BeliefStore(self.database).create(
            self.subject_id,
            "Blob parser belief fixture.",
            confidence=0.6,
            scope="integrity",
            supporting_event_ids=(self.event.event_id,),
        )
        goal = GoalStore(self.database).create_candidate(
            self.subject_id,
            self.candidate("Blob parser goal fixture"),
            causal_source_ids=(self.event.event_id,),
            reason="integrity fixture",
        )
        relationship = RelationshipStore(self.database).ensure(
            self.subject_id,
            "human",
            "blob-parser-human",
            "Blob parser collaborator",
            source_event_ids=(self.event.event_id,),
        )
        causal_link = CausalStore(self.database).links_from(
            self.subject_id, "event", self.event.event_id
        )[0]
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_appraisal_update")
            connection.execute("DROP TRIGGER prevent_affect_transition_update")
            connection.execute("DROP TRIGGER prevent_psychological_snapshot_update")
            connection.execute("DROP TRIGGER prevent_causal_link_update")
        cases = (
            ("memories", "memory_id", memory.memory_id, "salience"),
            ("memories", "memory_id", memory.memory_id, "current_revision"),
            ("beliefs", "belief_id", belief.belief_id, "confidence"),
            ("beliefs", "belief_id", belief.belief_id, "current_revision"),
            ("goals", "goal_id", goal.goal_id, "priority"),
            ("goals", "goal_id", goal.goal_id, "current_revision"),
            (
                "relationships",
                "relationship_id",
                relationship.relationship_id,
                "trust",
            ),
            (
                "relationships",
                "relationship_id",
                relationship.relationship_id,
                "current_revision",
            ),
            (
                "relationships",
                "relationship_id",
                relationship.relationship_id,
                "boundaries_json",
            ),
            ("causal_links", "link_id", causal_link.link_id, "strength"),
            ("causal_links", "link_id", causal_link.link_id, "metadata_json"),
            ("appraisals", "appraisal_id", result.appraisal.appraisal_id, "novelty"),
            ("affect_components", "subject_id", self.subject_id, "intensity"),
            (
                "affect_transitions",
                "transition_id",
                result.transitions[0].transition_id,
                "impulse",
            ),
            (
                "psychological_snapshots",
                "snapshot_id",
                result.snapshot.snapshot_id,
                "version",
            ),
            (
                "psychological_snapshots",
                "snapshot_id",
                result.snapshot.snapshot_id,
                "state_json",
            ),
        )
        for table, key, identifier, column in cases:
            with self.subTest(table=table, column=column):
                with self.database.connection() as connection:
                    original = connection.execute(
                        f'SELECT "{column}" FROM "{table}" WHERE "{key}" = ?',
                        (identifier,),
                    ).fetchone()[0]
                blob = sqlite3.Binary(str(original).encode("ascii"))
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key}" = ?',
                        (blob, identifier),
                    )
                with self.assertRaises(IntegrityError):
                    self.engine.verify_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{key}" = ?',
                        (original, identifier),
                    )

    def test_integrity_audit_rejects_nonfinite_durable_floats(self) -> None:
        belief = BeliefStore(self.database).create(
            self.subject_id,
            "Finite confidence fixture.",
            confidence=0.7,
            scope="test",
            supporting_event_ids=(self.event.event_id,),
        )
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_belief_revision_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE belief_revisions SET confidence = 'nan' WHERE belief_id = ?",
                (belief.belief_id,),
            )
        with self.assertRaises(IntegrityError):
            self.engine.verify_integrity(self.subject_id)

    def test_causal_links_reject_dangling_entities_and_mutation(self) -> None:
        store = CausalStore(self.database)
        with self.assertRaises(CausalValidationError):
            store.add(
                self.subject_id,
                "event",
                "missing",
                "caused",
                "event",
                self.event.event_id,
                strength=1.0,
            )
        result = self.engine.process_event(
            self.subject_id,
            self.event.event_id,
            self.appraisal(),
            [self.impulse()],
        )
        link = store.links_from(self.subject_id, "event", self.event.event_id)[0]
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE causal_links SET relation = 'rewritten' WHERE link_id = ?",
                (link.link_id,),
            )
        self.assertEqual(result.appraisal.event_id, self.event.event_id)


if __name__ == "__main__":
    unittest.main()
