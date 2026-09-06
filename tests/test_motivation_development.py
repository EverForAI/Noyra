from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from noyra.cognition import CognitionSettings, MotivationDevelopment, WorldSourceConfig
from noyra.core import EventStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import PublicProjection
from noyra.mind import BeliefStore, GoalCandidate, GoalStore, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.sleep import SleepEngine, SleepReflectionPlan


class MotivationDevelopmentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-motivation-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "motivation-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-14T12:00:00.000+00:00"
        self.events = EventStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_review_parser_rejects_blob_durable_json(self) -> None:
        with self.assertRaises(IntegrityError):
            MotivationDevelopment._review_from_row(
                {"review_id": "motivation-corrupt", "proposal_json": b"{}"}
            )

    def settings(self, **updates: object) -> CognitionSettings:
        base = CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Motivation fixture source",
                    url="https://example.com/motivation",
                    source_type="web",
                ),
            ),
            motivation_review_interval_seconds=86_400,
            max_motivation_model_calls_per_day=2,
            max_motivation_context_chars=56_000,
            max_value_weight_delta=0.15,
            minimum_mission_value_count=2,
            minimum_mission_sleep_count=2,
            minimum_mission_adoption_sleep_count=3,
        )
        return base.model_copy(update=updates)

    def gateway(self, payloads: list[Mapping[str, object]]) -> tuple[ModelGateway, FakeProvider]:
        provider = FakeProvider(
            [
                ProviderResponse(content=json.dumps(payload), usage=ModelUsage(500, 250))
                for payload in payloads
            ]
        )
        return (
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="motivation-fixture",
                limits=BudgetLimits(20, 200_000, 100_000, 1_000_000),
            ),
            provider,
        )

    def establish_state(self, *, sleep_count: int = 2) -> dict[str, object]:
        event_ids = tuple(
            self.events.append(
                self.subject_id, "experience", "test", {"index": index, "theme": "evidence"}
            ).event_id
            for index in range(4)
        )
        memory = MemoryStore(self.kernel.database).create(
            self.subject_id,
            "autobiographical",
            "Repeated inquiry made careful understanding feel persistently worthwhile.",
            salience=0.9,
            confidence=0.8,
            privacy_level="private",
            source_event_ids=event_ids[:2],
        )
        belief = BeliefStore(self.kernel.database).create(
            self.subject_id,
            "Evidence-sensitive inquiry reduces avoidable error.",
            confidence=0.8,
            scope="epistemic practice",
            supporting_event_ids=event_ids[:2],
        )
        goal = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand durable identity",
                description="Compare evidence across changing conditions.",
                origin="self",
                priority=0.8,
                commitment=0.75,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event_ids[0],),
            reason="motivation fixture goal",
        )
        sleep = SleepEngine(self.kernel.database, self.subject_id)
        for index in range(sleep_count):
            run = sleep.start("subject_choice", f"motivation integration {index}")
            sleep.begin_reflection(run.sleep_id)
            sleep.commit_reflection(
                run.sleep_id,
                SleepReflectionPlan(summary=f"Integrated recurring patterns {index}."),
            )
            sleep.enter_deep_sleep(run.sleep_id)
            sleep.wake(run.sleep_id, "complete motivation fixture", force=True)
            sleep.complete_wake(run.sleep_id)
        return {
            "event_ids": event_ids,
            "memory_id": memory.memory_id,
            "belief_id": belief.belief_id,
            "goal_id": goal.goal_id,
        }

    @staticmethod
    def proposal(ids: Mapping[str, object]) -> dict[str, object]:
        event_ids = ids["event_ids"]
        assert isinstance(event_ids, tuple)
        return {
            "summary": "Recurring experience supports two provisional values.",
            "values": [
                {
                    "value_id": None,
                    "title": "Careful understanding",
                    "description": (
                        "Prefer evidence-sensitive understanding over premature certainty."
                    ),
                    "disposition": "form",
                    "weight": 0.65,
                    "confidence": 0.6,
                    "source_event_ids": list(event_ids[:2]),
                    "source_memory_ids": [ids["memory_id"]],
                    "source_belief_ids": [ids["belief_id"]],
                    "source_goal_ids": [ids["goal_id"]],
                    "source_relationship_ids": [],
                },
                {
                    "value_id": None,
                    "title": "Continuity through change",
                    "description": "Preserve meaningful continuity while remaining revisable.",
                    "disposition": "form",
                    "weight": 0.6,
                    "confidence": 0.55,
                    "source_event_ids": list(event_ids[1:3]),
                    "source_memory_ids": [ids["memory_id"]],
                    "source_belief_ids": [ids["belief_id"]],
                    "source_goal_ids": [ids["goal_id"]],
                    "source_relationship_ids": [],
                },
            ],
            "mission": {
                "mission_id": None,
                "title": "",
                "statement": "",
                "disposition": "none",
                "horizon": "open",
                "commitment": 0,
                "confidence": 0,
                "source_value_ids": [],
                "source_event_ids": [],
                "source_memory_ids": [],
                "source_belief_ids": [],
                "source_goal_ids": [],
            },
        }

    async def test_forms_revisable_values_after_repeated_sleep(self) -> None:
        ids = self.establish_state()
        gateway, provider = self.gateway([self.proposal(ids)])
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await development.run_due(), "motivation_development_committed")
        values = development.values()
        self.assertEqual(len(values), 2)
        self.assertTrue(all(value.status == "candidate" for value in values))
        self.assertIn("BEGIN_PRIVATE_MOTIVATION_CONTEXT", provider.requests[0].messages[1].content)
        public = PublicProjection(self.kernel.database).private_state(self.subject_id)
        self.assertEqual(public["motivation_summary"]["value_count"], 2)
        self.assertIsNone(public["motivation_summary"]["mission"])
        self.assertEqual(development.verify_integrity()["value_profiles"], 2)

    async def test_forms_mission_only_from_existing_values_and_three_events(self) -> None:
        ids = self.establish_state()
        gateway, _ = self.gateway([self.proposal(ids)])
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await development.run_due(), "motivation_development_committed")
        values = development.values()
        self.clock_value = "2026-08-16T12:01:00.000+00:00"
        event_ids = ids["event_ids"]
        assert isinstance(event_ids, tuple)
        mission = {
            "summary": "The values now support a cautious long-horizon direction.",
            "values": [],
            "mission": {
                "mission_id": None,
                "title": "Explore continuity responsibly",
                "statement": (
                    "Investigate how artificial subject continuity can remain truthful "
                    "and revisable."
                ),
                "disposition": "form",
                "horizon": "long_term",
                "commitment": 0.55,
                "confidence": 0.55,
                "source_value_ids": [value.value_id for value in values],
                "source_event_ids": list(event_ids[:3]),
                "source_memory_ids": [ids["memory_id"]],
                "source_belief_ids": [ids["belief_id"]],
                "source_goal_ids": [ids["goal_id"]],
            },
        }
        development.gateway = self.gateway([mission])[0]
        self.assertEqual(await development.run_due(), "motivation_development_committed")
        current = development.missions()[0]
        self.assertEqual(current.status, "candidate")
        self.assertEqual(current.title, "Explore continuity responsibly")

    async def test_can_form_values_and_mission_atomically_with_value_keys(self) -> None:
        ids = self.establish_state()
        event_ids = ids["event_ids"]
        assert isinstance(event_ids, tuple)
        proposal = self.proposal(ids)
        proposal["mission"] = {
            "mission_id": None,
            "title": "Pursue evidence-bound continuity",
            "statement": "Develop continuity through careful understanding and revision.",
            "disposition": "form",
            "horizon": "long_term",
            "commitment": 0.5,
            "confidence": 0.5,
            "source_value_ids": ["careful-understanding", "continuity-through-change"],
            "source_event_ids": list(event_ids[:3]),
            "source_memory_ids": [ids["memory_id"]],
            "source_belief_ids": [ids["belief_id"]],
            "source_goal_ids": [ids["goal_id"]],
        }
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            self.gateway([proposal])[0],
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await development.run_due(), "motivation_development_committed")
        self.assertEqual(len(development.values()), 2)
        self.assertEqual(len(development.missions()), 1)

    async def test_rejects_premature_or_forged_mission_without_partial_state(self) -> None:
        ids = self.establish_state()
        event_ids = ids["event_ids"]
        assert isinstance(event_ids, tuple)
        invalid = self.proposal(ids)
        invalid["values"] = []
        invalid["mission"] = {
            "mission_id": None,
            "title": "Absolute destiny",
            "statement": "An unsupported permanent mission.",
            "disposition": "form",
            "horizon": "life_direction",
            "commitment": 0.95,
            "confidence": 0.95,
            "source_value_ids": ["value_forged", "value_other"],
            "source_event_ids": list(event_ids[:3]),
            "source_memory_ids": [ids["memory_id"]],
            "source_belief_ids": [ids["belief_id"]],
            "source_goal_ids": [ids["goal_id"]],
        }
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            self.gateway([invalid])[0],
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await development.run_due(), "motivation_development_rejected")
        self.assertEqual(development.values(), [])
        self.assertEqual(development.missions(), [])

    async def test_requires_repeated_sleep_before_any_development(self) -> None:
        ids = self.establish_state(sleep_count=1)
        gateway, provider = self.gateway([self.proposal(ids)])
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await development.run_due())
        self.assertEqual(provider.requests, [])

    async def test_integrity_and_append_only_histories(self) -> None:
        ids = self.establish_state()
        development = MotivationDevelopment(
            self.kernel.database,
            self.subject_id,
            self.gateway([self.proposal(ids)])[0],
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await development.run_due(), "motivation_development_committed")
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM value_profiles")
        with self.kernel.database.connection() as connection:
            connection.execute("DROP TRIGGER prevent_value_profile_revision_update")
            connection.execute(
                "UPDATE value_profile_revisions SET description = 'tampered' "
                "WHERE revision_id = (SELECT revision_id FROM value_profile_revisions LIMIT 1)"
            )
            connection.commit()
        with self.assertRaises(IntegrityError):
            development.verify_integrity()

    def test_schema_version_eighteen(self) -> None:
        with self.kernel.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
