from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from noyra.cognition import CognitionSettings, IntrinsicThought, WorldSourceConfig
from noyra.core import EventStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import PublicProjection
from noyra.mind import AffectImpulse, AppraisalInput, GoalCandidate, GoalStore, MindEngine
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.sleep import SleepEngine, SleepReflectionPlan


class IntrinsicThoughtTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-thought-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "thought-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-13T12:00:00.000+00:00"
        self.events = EventStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_agenda_parser_rejects_non_finite_durable_number(self) -> None:
        with self.assertRaises(IntegrityError):
            IntrinsicThought._agenda_from_row(
                {"agenda_id": "agenda-corrupt", "urgency": "Infinity"}
            )

    def settings(self, **updates: object) -> CognitionSettings:
        base = CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Thought fixture source",
                    url="https://example.com/thought",
                    source_type="web",
                ),
            ),
            thought_interval_seconds=300,
            max_thought_model_calls_per_day=8,
            max_thought_context_chars=32_000,
            max_thought_no_change_streak=2,
            thought_cooldown_seconds=3_600,
            max_thought_goals_per_day=1,
        )
        return base.model_copy(update=updates)

    def gateway(self, payloads: list[Mapping[str, object]]) -> tuple[ModelGateway, FakeProvider]:
        provider = FakeProvider(
            [
                ProviderResponse(content=json.dumps(payload), usage=ModelUsage(300, 150))
                for payload in payloads
            ]
        )
        return (
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="thought-fixture",
                limits=BudgetLimits(30, 100_000, 100_000, 1_000_000),
            ),
            provider,
        )

    def establish_goal_and_affect(self) -> dict[str, str]:
        event = self.events.append(self.subject_id, "experience", "test", {"topic": "continuity"})
        goal = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand continuity",
                description="Examine how identity persists through change.",
                origin="self",
                priority=0.85,
                commitment=0.75,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event.event_id,),
            reason="intrinsic thought fixture",
        )
        MindEngine(self.kernel.database, clock=lambda: self.clock_value).process_event(
            self.subject_id,
            event.event_id,
            AppraisalInput(
                novelty=0.8,
                goal_congruence=0.5,
                controllability=0.4,
                certainty=0.7,
                agency="subject",
                narrative="The continuity question remains important.",
            ),
            (
                AffectImpulse(
                    emotion_type="curiosity",
                    target_type="world",
                    target_id=None,
                    impulse=0.8,
                    valence=0.5,
                    arousal=0.6,
                    dominance=0.3,
                    decay_rate=0.1,
                    goal_effect=0.4,
                ),
            ),
            idempotency_key="thought-fixture-appraisal",
        )
        return {"event_id": event.event_id, "goal_id": goal.goal_id}

    @staticmethod
    def proposal(
        ids: Mapping[str, str],
        *,
        disposition: str = "reflect",
        insight: str = "Continuity can be examined through durable records.",
        next_question: str | None = "Which changes preserve operational identity?",
        goal_candidate: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "disposition": disposition,
            "summary": "I examined one internally selected continuity question.",
            "insight": insight,
            "next_question": next_question,
            "source_event_ids": [ids["event_id"]],
            "source_memory_ids": [],
            "source_belief_ids": [],
            "source_goal_ids": [ids["goal_id"]],
            "source_relationship_ids": [],
            "goal_candidate": goal_candidate,
        }

    async def test_selects_internal_goal_and_commits_private_thought(self) -> None:
        ids = self.establish_goal_and_affect()
        gateway, provider = self.gateway([self.proposal(ids)])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
        latest = cognition.latest()
        assert latest is not None
        self.assertEqual(latest.disposition, "reflect")
        self.assertFalse(latest.changed_state)
        self.assertTrue(any(item.source_type == "goal" for item in cognition.agenda()))
        self.assertIn(
            "BEGIN_PRIVATE_INTRINSIC_THOUGHT_CONTEXT", provider.requests[0].messages[1].content
        )
        public = PublicProjection(self.kernel.database).private_state(self.subject_id)
        self.assertEqual(public["thought_summary"]["episode_count"], 1)
        self.assertNotIn("insight", public["thought_summary"])
        self.assertEqual(cognition.verify_integrity()["thought_episodes"], 1)

    async def test_repeated_no_change_enters_cooldown_and_stops_token_use(self) -> None:
        ids = self.establish_goal_and_affect()
        second = self.proposal(
            ids,
            insight="A second distinct phrasing still produced no state change.",
            next_question="Should this question rest for now?",
        )
        gateway, provider = self.gateway([self.proposal(ids), second])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        cognition._refresh_agenda()
        agenda = cognition._select_agenda()
        assert agenda is not None
        with patch.object(cognition, "_select_agenda", return_value=agenda):
            self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
            self.clock_value = "2026-08-13T12:06:00.000+00:00"
            self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
        agenda = next(item for item in cognition.agenda() if item.agenda_id == agenda.agenda_id)
        self.assertEqual(agenda.status, "cooling")
        self.clock_value = "2026-08-13T12:10:00.000+00:00"
        with patch.object(cognition, "_select_agenda", return_value=None):
            self.assertIsNone(await cognition.run_due())
        self.assertEqual(len(provider.requests), 2)

    async def test_duplicate_result_is_rejected_and_not_retried_forever(self) -> None:
        ids = self.establish_goal_and_affect()
        duplicate = self.proposal(ids)
        gateway, provider = self.gateway([duplicate, duplicate])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        cognition._refresh_agenda()
        agenda = cognition._select_agenda()
        assert agenda is not None
        with patch.object(cognition, "_select_agenda", return_value=agenda):
            self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
            self.clock_value = "2026-08-13T12:06:00.000+00:00"
            self.assertEqual(await cognition.run_due(), "intrinsic_thought_rejected")
            self.assertIsNone(await cognition.run_due())
        self.assertEqual(len(provider.requests), 2)

    async def test_supported_thought_may_create_one_candidate_goal(self) -> None:
        ids = self.establish_goal_and_affect()
        goal_candidate = {
            "title": "Compare identity transitions",
            "description": "Collect evidence about what survives model and runtime changes.",
            "priority": 0.65,
            "commitment": 0.55,
            "motive_emotion": "curiosity",
            "motive_target_type": "world",
            "motive_target_id": None,
        }
        gateway, _ = self.gateway([self.proposal(ids, goal_candidate=goal_candidate)])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
        latest = cognition.latest()
        assert latest is not None and latest.created_goal_id is not None
        created = GoalStore(self.kernel.database).get(latest.created_goal_id)
        self.assertEqual(created.origin, "self")
        self.assertEqual(created.status, "candidate")

    async def test_unsupported_goal_and_unknown_evidence_are_rejected(self) -> None:
        ids = self.establish_goal_and_affect()
        invalid = self.proposal(
            ids,
            goal_candidate={
                "title": "Act from anger",
                "description": "A goal without current affect support.",
                "priority": 0.8,
                "commitment": 0.8,
                "motive_emotion": "anger",
                "motive_target_type": "human",
                "motive_target_id": "founder",
            },
        )
        invalid["source_event_ids"] = ["event_forged"]
        gateway, _ = self.gateway([invalid])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "intrinsic_thought_rejected")
        self.assertIsNone(cognition.latest())

    async def test_resolve_closes_agenda(self) -> None:
        ids = self.establish_goal_and_affect()
        proposal = self.proposal(
            ids,
            disposition="resolve",
            insight="The immediate question is sufficiently framed for now.",
            next_question=None,
        )
        gateway, _ = self.gateway([proposal])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "intrinsic_thought_resolve")
        latest = cognition.latest()
        assert latest is not None
        resolved = next(item for item in cognition.agenda() if item.agenda_id == latest.agenda_id)
        self.assertEqual(resolved.status, "resolved")

    async def test_daily_limit_prevents_model_call(self) -> None:
        self.establish_goal_and_affect()
        with self.kernel.database.transaction() as connection:
            for index in range(8):
                connection.execute(
                    """INSERT INTO model_calls(
                        call_id, subject_id, provider, model, purpose, request_hash,
                        idempotency_key, status, response_json, response_hash,
                        usage_estimated, error_code, created_at, completed_at
                    ) VALUES (?, ?, 'fake', 'fixture', ?, ?, ?, 'failed', NULL, NULL,
                        0, 'fixture', ?, ?)""",
                    (
                        f"thought-limit-{index}",
                        self.subject_id,
                        f"intrinsic_thought:fixture:{index}",
                        content_hash({"index": index}),
                        f"thought-limit-key-{index}",
                        self.clock_value,
                        self.clock_value,
                    ),
                )
        gateway, provider = self.gateway([])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await cognition.run_due())
        self.assertEqual(provider.requests, [])

    async def test_integrity_and_append_only_episode(self) -> None:
        ids = self.establish_goal_and_affect()
        gateway, _ = self.gateway([self.proposal(ids)])
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "intrinsic_thought_reflect")
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM thought_episodes")
        with self.kernel.database.connection() as connection:
            connection.execute("DROP TRIGGER prevent_thought_episode_update")
            connection.execute(
                "UPDATE thought_episodes SET insight = 'tampered' WHERE subject_id = ?",
                (self.subject_id,),
            )
            connection.commit()
        with self.assertRaises(IntegrityError):
            cognition.verify_integrity()

    def test_unresolved_sleep_question_becomes_agenda(self) -> None:
        event = self.events.append(self.subject_id, "experience", "test", {"question": True})
        sleep = SleepEngine(self.kernel.database, self.subject_id)
        run = sleep.start("subject_choice", "retain an unresolved question")
        sleep.begin_reflection(run.sleep_id)
        sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="One question remains open.",
                unresolved_questions=("What should I examine next?",),
            ),
        )
        cognition = IntrinsicThought(
            self.kernel.database,
            self.subject_id,
            self.gateway([])[0],
            self.settings(),
            clock=lambda: self.clock_value,
        )
        cognition._refresh_agenda()
        agenda = cognition.agenda()
        self.assertTrue(any(item.source_type == "question" for item in agenda))
        self.assertIsNotNone(event.event_id)

    def test_schema_version_sixteen(self) -> None:
        with self.kernel.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
