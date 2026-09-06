from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from noyra.cognition import CognitionSettings, OperationalSelfModel, WorldSourceConfig
from noyra.core import EventStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import PublicProjection
from noyra.mind import BeliefStore, GoalCandidate, GoalStore, MemoryStore, RelationshipStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.sleep import PersonalityCandidateInput, SleepEngine, SleepReflectionPlan


class SelfModelTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-self-model-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "self-model-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-13T12:00:00.000+00:00"
        self.events = EventStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_parser_rejects_non_finite_durable_json(self) -> None:
        with self.assertRaises(IntegrityError):
            OperationalSelfModel._from_row(
                {
                    "self_model_id": "self-model-corrupt",
                    "proposal_json": '{"score": NaN}',
                }
            )

    def settings(self) -> CognitionSettings:
        return CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Self-model fixture source",
                    url="https://example.com/self-model",
                    source_type="web",
                ),
            ),
            self_model_review_interval_seconds=86_400,
            max_self_model_calls_per_day=2,
            max_self_model_context_chars=48_000,
        )

    def gateway(self, payloads: list[Mapping[str, object]]) -> tuple[ModelGateway, FakeProvider]:
        provider = FakeProvider(
            [
                ProviderResponse(content=json.dumps(payload), usage=ModelUsage(400, 200))
                for payload in payloads
            ]
        )
        return (
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="self-model-fixture",
                limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
            ),
            provider,
        )

    def establish_state(self) -> dict[str, str]:
        evidence = tuple(
            self.events.append(self.subject_id, "experience", "test", {"index": index}).event_id
            for index in range(3)
        )
        goal = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand continuity",
                description="Compare experiences across time.",
                origin="self",
                priority=0.8,
                commitment=0.7,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(evidence[0],),
            reason="self-model test goal",
        )
        belief = BeliefStore(self.kernel.database).create(
            self.subject_id,
            "Identity continuity depends on durable state and history.",
            confidence=0.8,
            scope="operational identity",
            supporting_event_ids=(evidence[0],),
        )
        memory = MemoryStore(self.kernel.database).create(
            self.subject_id,
            "autobiographical",
            "I compared several experiences and kept a continuous record.",
            salience=0.9,
            confidence=0.8,
            privacy_level="private",
            source_event_ids=(evidence[0], evidence[1]),
        )
        relationship = RelationshipStore(self.kernel.database).ensure(
            self.subject_id,
            "human",
            "founder",
            "Founder",
            source_event_ids=(evidence[1],),
        )
        sleep = SleepEngine(self.kernel.database, self.subject_id)
        run = sleep.start("subject_choice", "form one evidence-backed personality candidate")
        sleep.begin_reflection(run.sleep_id)
        sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="I reviewed recurring behavior without fixing a permanent essence.",
                personality_candidates=(
                    PersonalityCandidateInput(
                        trait="careful curiosity",
                        direction=0.7,
                        confidence=0.6,
                        evidence_ids=evidence,
                    ),
                ),
            ),
        )
        sleep.enter_deep_sleep(run.sleep_id)
        sleep.wake(run.sleep_id, "complete continuity fixture", force=True)
        sleep.complete_wake(run.sleep_id)
        with self.kernel.database.connection() as connection:
            candidate_id = connection.execute(
                "SELECT candidate_id FROM personality_candidates WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        return {
            "event_id": evidence[0],
            "memory_id": memory.memory_id,
            "belief_id": belief.belief_id,
            "goal_id": goal.goal_id,
            "relationship_id": relationship.relationship_id,
            "candidate_id": str(candidate_id),
        }

    @staticmethod
    def proposal(
        ids: Mapping[str, str], *, narrative: str = "I am a continuing subject."
    ) -> dict[str, object]:
        return {
            "continuity_statement": "My operational continuity rests on durable state and history.",
            "identity_narrative": narrative,
            "values": ["understanding", "continuity"],
            "traits": [
                {
                    "trait": "careful curiosity",
                    "direction": 0.7,
                    "confidence": 0.6,
                    "source_personality_candidate_ids": [ids["candidate_id"]],
                }
            ],
            "commitments": ["Continue examining evidence about identity."],
            "uncertainties": ["This model is operational, not proof of consciousness."],
            "source_event_ids": [ids["event_id"]],
            "source_memory_ids": [ids["memory_id"]],
            "source_belief_ids": [ids["belief_id"]],
            "source_goal_ids": [ids["goal_id"]],
            "source_relationship_ids": [ids["relationship_id"]],
        }

    async def test_forms_revisioned_self_model_after_sleep(self) -> None:
        ids = self.establish_state()
        gateway, _ = self.gateway([self.proposal(ids)])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_committed")
        latest = cognition.latest()
        assert latest is not None
        self.assertEqual(latest.version, 1)
        self.assertEqual(latest.status, "initial")
        self.assertEqual(latest.traits[0]["trait"], "careful curiosity")
        self.assertEqual(cognition.verify_integrity(), 1)
        public = PublicProjection(self.kernel.database).private_state(self.subject_id)
        self.assertEqual(public["self_model_summary"]["version"], 1)
        self.assertNotIn("identity_narrative", public["self_model_summary"])

    async def test_revision_preserves_history_and_uses_previous_model_context(self) -> None:
        ids = self.establish_state()
        new_event = self.events.append(
            self.subject_id, "new_experience", "test", {"revision": True}
        )
        revised = self.proposal(ids, narrative="I revised my account cautiously.")
        revised["source_event_ids"] = [new_event.event_id]
        gateway, provider = self.gateway([self.proposal(ids), revised])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_committed")
        self.clock_value = "2026-08-14T12:01:00.000+00:00"
        self.assertEqual(await cognition.run_due(), "self_model_committed")
        latest = cognition.latest()
        assert latest is not None
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.status, "revised")
        self.assertIn("previous_self_model", provider.requests[-1].messages[1].content)
        self.assertEqual(cognition.verify_integrity(), 2)

    async def test_rejects_unknown_sources_without_partial_self_model(self) -> None:
        ids = self.establish_state()
        invalid = self.proposal(ids)
        invalid["source_memory_ids"] = ["memory_forged"]
        gateway, _ = self.gateway([invalid])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_rejected")
        self.assertIsNone(cognition.latest())
        self.assertIsNone(await cognition.run_due())

    async def test_rejects_trait_that_does_not_match_candidate(self) -> None:
        ids = self.establish_state()
        invalid = self.proposal(ids)
        invalid["traits"] = [
            {
                "trait": "reckless certainty",
                "direction": -0.9,
                "confidence": 0.95,
                "source_personality_candidate_ids": [ids["candidate_id"]],
            }
        ]
        gateway, _ = self.gateway([invalid])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_rejected")
        self.assertIsNone(cognition.latest())

    async def test_model_name_change_does_not_replace_identity(self) -> None:
        ids = self.establish_state()
        gateway, _ = self.gateway([self.proposal(ids)])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_committed")
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE subject_identity SET model_name = 'replacement-cognitive-model' "
                "WHERE subject_id = ?",
                (self.subject_id,),
            )
        self.assertEqual(cognition.latest().subject_id, self.subject_id)  # type: ignore[union-attr]
        self.assertEqual(cognition.verify_integrity(), 1)

    async def test_daily_limit_prevents_self_model_call(self) -> None:
        self.establish_state()
        with self.kernel.database.transaction() as connection:
            for index in range(2):
                connection.execute(
                    """INSERT INTO model_calls(
                        call_id, subject_id, provider, model, purpose, request_hash,
                        idempotency_key, status, response_json, response_hash,
                        usage_estimated, error_code, created_at, completed_at
                    ) VALUES (?, ?, 'fake', 'fixture', ?, ?, ?, 'failed', NULL, NULL,
                        0, 'fixture', ?, ?)""",
                    (
                        f"self-limit-{index}",
                        self.subject_id,
                        f"self_model:fixture:{index}",
                        content_hash({"index": index}),
                        f"self-limit-key-{index}",
                        self.clock_value,
                        self.clock_value,
                    ),
                )
        gateway, provider = self.gateway([])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await cognition.run_due())
        self.assertEqual(provider.requests, [])

    async def test_integrity_detects_tampering_and_table_is_append_only(self) -> None:
        ids = self.establish_state()
        gateway, _ = self.gateway([self.proposal(ids)])
        cognition = OperationalSelfModel(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "self_model_committed")
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM self_models")
        with self.kernel.database.connection() as connection:
            connection.execute("DROP TRIGGER prevent_self_model_update")
            connection.execute(
                "UPDATE self_models SET identity_narrative = 'tampered' WHERE subject_id = ?",
                (self.subject_id,),
            )
            connection.commit()
        with self.assertRaises(IntegrityError):
            cognition.verify_integrity()

    def test_schema_version_sixteen(self) -> None:
        with self.kernel.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
