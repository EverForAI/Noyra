from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from noyra.cognition import CognitionSettings, MetacognitiveControl, WorldSourceConfig
from noyra.core import EventStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id
from noyra.interaction import PublicProjection
from noyra.mind import GoalCandidate, GoalStore, MindEngine
from noyra.mind.types import AffectImpulse, AppraisalInput
from noyra.model import ModelLedger


class MetacognitiveControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-metacognition-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "metacognition-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-13T12:00:00.000+00:00"
        self.events = EventStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def settings(self, **updates: object) -> CognitionSettings:
        base = CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Metacognition fixture source",
                    url="https://example.com/meta",
                    source_type="web",
                ),
            ),
            thought_interval_seconds=300,
            research_interval_seconds=300,
            goal_governance_interval_seconds=300,
            social_review_interval_seconds=300,
            metacognitive_sleep_threshold=0.82,
            metacognitive_fixation_penalty=0.2,
        )
        return base.model_copy(update=updates)

    def control(self, **updates: object) -> MetacognitiveControl:
        return MetacognitiveControl(
            self.kernel.database,
            self.subject_id,
            self.settings(**updates),
            clock=lambda: self.clock_value,
        )

    def establish_goal(self) -> str:
        event = self.events.append(
            self.subject_id,
            "experience",
            "test",
            {"topic": "continuity"},
            occurred_at=self.clock_value,
        )
        goal = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand continuity",
                description="Seek evidence about persistent identity.",
                origin="self",
                priority=0.9,
                commitment=0.8,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event.event_id,),
            reason="metacognitive fixture",
        )
        GoalStore(self.kernel.database).revise(
            goal.goal_id,
            status="active",
            priority=goal.priority,
            commitment=goal.commitment,
            progress=goal.progress,
            emotional_pressure=goal.emotional_pressure,
            reason="activate fixture goal",
            causal_source_ids=(event.event_id,),
            expected_revision=goal.current_revision,
        )
        MindEngine(self.kernel.database, clock=lambda: self.clock_value).process_event(
            self.subject_id,
            event.event_id,
            AppraisalInput(
                novelty=0.9,
                goal_congruence=0.5,
                controllability=0.4,
                certainty=0.4,
                agency="subject",
                narrative="The question remains open.",
            ),
            (
                AffectImpulse(
                    emotion_type="curiosity",
                    target_type="world",
                    target_id=None,
                    impulse=0.9,
                    valence=0.5,
                    arousal=0.7,
                    dominance=0.4,
                    decay_rate=0.1,
                    goal_effect=0.4,
                ),
            ),
            idempotency_key="metacognitive-fixture-appraisal",
        )
        return goal.goal_id

    def insert_agenda(self, *, no_change: int = 0) -> str:
        agenda_id = new_id("agenda")
        state_hash = content_hash(
            {
                "source_type": "goal",
                "source_id": "goal_attention",
                "topic": "An unresolved identity question",
                "urgency": 0.9,
                "novelty": 0.9,
                "emotional_weight": 0.8,
                "recurrence_count": 1,
                "consecutive_no_change": no_change,
                "status": "open",
                "cooldown_until": None,
            }
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO thought_agenda_items(
                    agenda_id, subject_id, source_type, source_id, topic, urgency, novelty,
                    emotional_weight, recurrence_count, consecutive_no_change, status,
                    cooldown_until, state_hash, created_at, updated_at
                ) VALUES (?, ?, 'goal', 'goal_attention', ?, 0.9, 0.9, 0.8, 1, ?,
                    'open', NULL, ?, ?, ?)""",
                (
                    agenda_id,
                    self.subject_id,
                    "An unresolved identity question",
                    no_change,
                    state_hash,
                    self.clock_value,
                    self.clock_value,
                ),
            )
            connection.execute(
                """INSERT INTO thought_agenda_revisions(
                    revision_id, agenda_id, old_status, new_status, old_recurrence_count,
                    new_recurrence_count, old_consecutive_no_change,
                    new_consecutive_no_change, cooldown_until, reason, state_hash, created_at
                ) VALUES (?, ?, NULL, 'open', 0, 1, 0, ?, NULL, 'fixture', ?, ?)""",
                (
                    new_id("arev"),
                    agenda_id,
                    no_change,
                    content_hash(
                        {
                            "old_status": None,
                            "new_status": "open",
                            "old_recurrence_count": 0,
                            "new_recurrence_count": 1,
                            "old_consecutive_no_change": 0,
                            "new_consecutive_no_change": no_change,
                            "cooldown_until": None,
                            "reason": "fixture",
                        }
                    ),
                    self.clock_value,
                ),
            )
        return agenda_id

    def insert_successful_call(
        self, call_id: str, purpose: str, payload: Mapping[str, object]
    ) -> None:
        response = {
            "content": json.dumps(payload),
            "usage": {"input_tokens": 120, "output_tokens": 40},
            "cost_microusd": 0,
            "attempts": 1,
        }
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO model_calls(
                    call_id, subject_id, provider, model, purpose, request_hash,
                    idempotency_key, status, response_json, response_hash,
                    usage_estimated, error_code, created_at, completed_at
                ) VALUES (?, ?, 'fake', 'fixture', ?, ?, ?, 'succeeded', ?, ?, 0, NULL, ?, ?)""",
                (
                    call_id,
                    self.subject_id,
                    purpose,
                    content_hash({"purpose": purpose}),
                    f"key-{call_id}",
                    canonical_json(response),
                    content_hash(response),
                    self.clock_value,
                    self.clock_value,
                ),
            )

    def test_selects_high_value_thought_and_learns_productive_outcome(self) -> None:
        agenda_id = self.insert_agenda()
        control = self.control()
        decision = control.run_due()
        self.assertEqual(decision.strategy, "think")
        self.assertEqual(decision.target_id, agenda_id)
        call_id = "meta-thought-call"
        proposal: dict[str, object] = {
            "disposition": "reframe",
            "summary": "A distinct frame was found.",
            "insight": "Operational continuity can be tested through durable state.",
            "next_question": "Which state changes matter most?",
            "source_event_ids": [],
            "source_memory_ids": [],
            "source_belief_ids": [],
            "source_goal_ids": [],
            "source_relationship_ids": [],
            "goal_candidate": None,
        }
        self.insert_successful_call(call_id, f"intrinsic_thought:{agenda_id}:0", proposal)
        self.insert_thought_episode(agenda_id, call_id, proposal)
        control.record_result("intrinsic_thought_reframe")
        profile = control.profiles()[0]
        self.assertEqual(profile.strategy, "think")
        self.assertEqual(profile.productive, 1)
        self.assertEqual(profile.last_outcome, "productive")
        self.assertEqual(control.verify_integrity()["metacognitive_outcomes"], 1)

    def test_integrity_rejects_fractional_strategy_revision_integer(self) -> None:
        control = self.control()
        profile_id = new_id("cstrategy")
        revision_id = new_id("cstrategy-rev")
        profile_hash = control._profile_hash("think", 1, 1, 0, 0, 0.5, "productive")
        revision_hash = control._profile_revision_hash(
            1, 1, 0, 0, 0.5, "productive", "test", "source", "fixture"
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO cognitive_strategy_profiles(
                    profile_id, subject_id, strategy, attempts, productive, stagnant, failed,
                    confidence, last_outcome, state_hash, current_revision, created_at, updated_at
                ) VALUES (?, ?, 'think', 1, 1, 0, 0, 0.5, 'productive', ?, 1, ?, ?)""",
                (profile_id, self.subject_id, profile_hash, self.clock_value, self.clock_value),
            )
            connection.execute(
                """INSERT INTO cognitive_strategy_profile_revisions(
                    revision_id, profile_id, revision_number, attempts, productive, stagnant,
                    failed, confidence, last_outcome, source_type, source_id, reason,
                    state_hash, created_at
                ) VALUES (?, ?, 1, 1, 1, 0, 0, 0.5, 'productive', 'test', 'source',
                          'fixture', ?, ?)""",
                (revision_id, profile_id, revision_hash, self.clock_value),
            )
        with self.kernel.database.connection() as connection:
            connection.execute("DROP TRIGGER prevent_cognitive_strategy_revision_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE cognitive_strategy_profile_revisions SET attempts = 1.5 "
                "WHERE revision_id = ?",
                (revision_id,),
            )
            connection.commit()
        with self.assertRaises(IntegrityError):
            control.verify_integrity()

    def test_high_fatigue_selects_sleep_without_model_call(self) -> None:
        from noyra.sleep import FatigueInputs, FatigueTracker

        FatigueTracker(self.kernel.database).assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=0.7,
                cognitive_load=0.95,
                frustration=0.95,
                goal_conflict=0.9,
                staleness=0.9,
            ),
            reason="metacognitive sleep fixture",
        )
        decision = self.control().run_due()
        self.assertEqual(decision.strategy, "sleep")
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 0
            )

    def test_pending_outcome_forces_wait_until_deterministic_evaluation(self) -> None:
        goal_id = self.establish_goal()
        call_id = "meta-research-call"
        self.insert_successful_call(call_id, "research_plan:0", {"fixture": True})
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO research_search_runs(
                    research_id, subject_id, planner_call_id, goal_id, idempotency_key,
                    status, initial_method, final_method, provider_config_id, query_hash,
                    result_count, accepted_source_ids_json, rounds_json, plan_json, plan_hash,
                    state_hash, created_at, completed_at
                ) VALUES ('research_pending', ?, ?, ?, 'research-pending', 'no_results',
                    'model', 'model', NULL, ?, 0, '[]', '[]', '{}', ?, ?, ?, ?)""",
                (
                    self.subject_id,
                    call_id,
                    goal_id,
                    content_hash("query"),
                    content_hash({}),
                    content_hash({"fixture": "pending"}),
                    self.clock_value,
                    self.clock_value,
                ),
            )
        decision = self.control().run_due()
        self.assertEqual(decision.strategy, "wait")
        self.assertEqual(decision.reason_code, "durable_outcome_must_be_evaluated_first")

    def test_authenticated_projection_shows_aggregate_decision_not_private_state(self) -> None:
        self.control().run_due()
        public = PublicProjection(self.kernel.database).private_state(self.subject_id)
        self.assertEqual(public["metacognition_summary"]["decision_count"], 1)
        self.assertNotIn("uncertainty", public["metacognition_summary"]["latest"])
        self.assertNotIn("fixation_risk", public["metacognition_summary"]["latest"])

    def test_unresolved_decision_is_recovered_without_duplicate_choice(self) -> None:
        control = self.control()
        first = control.run_due()
        recovered = control.run_due()
        self.assertEqual(recovered.decision_id, first.decision_id)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM metacognitive_decisions").fetchone()[0],
                1,
            )

    def test_stale_unresolved_decision_is_quarantined_and_released(self) -> None:
        control = self.control(metacognitive_pending_timeout_seconds=60)
        first = control.run_due()
        self.clock_value = "2026-08-13T12:02:00.000+00:00"
        recovered = control.run_due()
        self.assertNotEqual(recovered.decision_id, first.decision_id)
        with self.kernel.database.connection() as connection:
            outcome = connection.execute(
                "SELECT outcome, rationale_code FROM metacognitive_outcomes WHERE decision_id = ?",
                (first.decision_id,),
            ).fetchone()
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(tuple(outcome), ("unknown", "workflow_timeout_quarantined"))

    def test_database_rejects_cross_subject_metacognitive_outcome(self) -> None:
        decision = self.control().run_due()
        other_subject = "Noyra-metacognition-other"
        from noyra.core.identity import IdentityStore

        IdentityStore(self.kernel.database).ensure(
            other_subject, content_hash({"seed": "other-subject"})
        )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                """INSERT INTO metacognitive_outcomes(
                    outcome_id, subject_id, decision_id, strategy, source_type, source_id,
                    outcome, token_cost, rationale_code, state_hash, created_at
                ) VALUES ('cross-subject-outcome', ?, ?, ?, 'test', 'source',
                    'unknown', 0, 'cross-subject', 'hash', ?)
                """,
                (
                    other_subject,
                    decision.decision_id,
                    decision.strategy,
                    self.clock_value,
                ),
            )

    def test_append_only_decisions_and_schema_seventeen(self) -> None:
        control = self.control()
        control.run_due()
        with self.kernel.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM metacognitive_decisions")

    def test_model_ledger_recovery_still_operates_with_new_schema(self) -> None:
        self.assertEqual(ModelLedger(self.kernel.database).recover_interrupted(self.subject_id), [])

    def insert_thought_episode(
        self, agenda_id: str, call_id: str, proposal: Mapping[str, object]
    ) -> None:
        from noyra.cognition.thought import IntrinsicThought

        proposal_hash = content_hash(proposal)
        result_hash = content_hash({"result": "distinct"})
        state_hash = IntrinsicThought._episode_hash(
            agenda_id,
            call_id,
            "reframe",
            str(proposal["summary"]),
            str(proposal["insight"]),
            str(proposal["next_question"]),
            (),
            (),
            (),
            (),
            (),
            proposal_hash,
            result_hash,
            True,
            None,
            self.clock_value,
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO thought_episodes(
                    thought_id, subject_id, agenda_id, model_call_id, idempotency_key,
                    disposition, summary, insight, next_question, source_event_ids_json,
                    source_memory_ids_json, source_belief_ids_json, source_goal_ids_json,
                    source_relationship_ids_json, proposal_json, proposal_hash, result_hash,
                    changed_state, created_goal_id, state_hash, created_at
                ) VALUES ('thought_meta', ?, ?, ?, 'thought-meta', 'reframe', ?, ?, ?,
                    '[]', '[]', '[]', '[]', '[]', ?, ?, ?, 1, NULL, ?, ?)""",
                (
                    self.subject_id,
                    agenda_id,
                    call_id,
                    proposal["summary"],
                    proposal["insight"],
                    proposal["next_question"],
                    canonical_json(proposal),
                    proposal_hash,
                    result_hash,
                    state_hash,
                    self.clock_value,
                ),
            )


if __name__ == "__main__":
    unittest.main()
