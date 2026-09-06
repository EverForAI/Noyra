from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.cognition import (
    CognitionSettings,
    ControlledSelfModification,
    SelfModificationError,
)
from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash


class ControlledSelfModificationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kernel = SubjectKernel(
            Path(self.temp.name) / "noyra.sqlite3",
            "Noyra-selfmod-test",
            content_hash({"seed": "selfmod"}),
        )
        self.kernel.boot()
        self.now = "2026-08-14T00:00:00.000+00:00"
        self.event = EventStore(self.kernel.database).append(
            self.kernel.subject_id,
            "metacognitive_outcome",
            "test",
            {"productive": False},
            occurred_at=self.now,
        )
        self.manager = ControlledSelfModification(
            self.kernel.database,
            self.kernel.subject_id,
            CognitionSettings(),
            clock=lambda: self.now,
            observation_seconds=3600,
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def _insert_outcome(
        self,
        sequence: int,
        strategy: str,
        outcome: str,
        token_cost: int,
        created_at: str,
    ) -> None:
        decision_id = f"decision-{sequence}"
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO metacognitive_decisions(
                    decision_id, subject_id, strategy, target_type, target_id, reason_code,
                    score, uncertainty, fixation_risk, resource_pressure, state_hash, created_at
                ) VALUES (?, ?, ?, 'none', NULL, 'test', 0.5, 0.5, 0.0, 0.0, ?, ?)""",
                (
                    decision_id,
                    self.kernel.subject_id,
                    strategy,
                    content_hash({"decision": sequence}),
                    created_at,
                ),
            )
            connection.execute(
                """INSERT INTO metacognitive_outcomes(
                    outcome_id, subject_id, decision_id, strategy, source_type, source_id,
                    outcome, token_cost, rationale_code, state_hash, created_at
                ) VALUES (?, ?, ?, ?, 'test', ?, ?, ?, 'test', ?, ?)""",
                (
                    f"outcome-{sequence}",
                    self.kernel.subject_id,
                    decision_id,
                    strategy,
                    f"source-{sequence}",
                    outcome,
                    token_cost,
                    content_hash({"outcome": sequence}),
                    created_at,
                ),
            )

    def test_safe_proposal_applies_and_rolls_back_after_harm(self) -> None:
        proposal = self.manager.propose(
            "thought_interval_seconds",
            1500.0,
            reason="reduce stagnant thought loops",
            evidence_ids=(self.event.event_id,),
        )
        self.assertEqual(proposal.status, "simulated")
        applied = self.manager.apply(proposal.proposal_id)
        self.assertEqual(applied.status, "applied")
        self.assertEqual(self.manager.effective("thought_interval_seconds"), 1500.0)
        rolled_back = self.manager.observe(proposal.proposal_id, productive=0, failed=2, stagnant=0)
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertEqual(self.manager.effective("thought_interval_seconds"), 1800.0)
        self.assertEqual(self.manager.verify_integrity()["self_modification_revisions"], 2)

    def test_integrity_rejects_non_finite_proposal_json(self) -> None:
        proposal = self.manager.propose(
            "thought_interval_seconds",
            1500.0,
            reason="durable parser fixture",
            evidence_ids=(self.event.event_id,),
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE self_modification_proposals SET validation_json = 'NaN' "
                "WHERE proposal_id = ?",
                (proposal.proposal_id,),
            )
        with self.assertRaises(IntegrityError):
            self.manager.verify_integrity()

    def test_protected_setting_and_large_delta_are_rejected(self) -> None:
        with self.assertRaises(SelfModificationError):
            self.manager.propose(
                "subject_id", 1, reason="forbidden", evidence_ids=(self.event.event_id,)
            )
        proposal = self.manager.propose(
            "max_search_rounds_per_run",
            4,
            reason="too large",
            evidence_ids=(self.event.event_id,),
        )
        self.assertEqual(proposal.status, "rejected")
        with self.assertRaises(SelfModificationError):
            self.manager.apply(proposal.proposal_id)

    def test_stagnation_mapping_targets_matching_strategy(self) -> None:
        self.assertEqual(
            ControlledSelfModification.STRATEGY_TO_SETTING,
            {
                "think": "max_thought_no_change_streak",
                "research": "research_interval_seconds",
            },
        )

        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO cognitive_strategy_profiles(
                    profile_id, subject_id, strategy, attempts, productive, stagnant, failed,
                    confidence, last_outcome, state_hash, current_revision, created_at, updated_at
                ) VALUES ('think-profile', ?, 'think', 5, 1, 4, 0, 0.2, 'stagnant',
                    'test-hash', 1, ?, ?)""",
                (self.kernel.subject_id, self.now, self.now),
            )
        candidate = self.manager._stagnant_strategy_candidate()
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate[0], "max_thought_no_change_streak")

        self._insert_outcome(1, "think", "productive", 20, "2026-08-14T00:01:00+00:00")
        self._insert_outcome(2, "goal_review", "failed", 20, "2026-08-14T00:02:00+00:00")
        self._insert_outcome(3, "think", "stagnant", 20, "2026-08-14T00:03:00+00:00")
        self.assertEqual(
            self.manager._outcomes_since(self.now, "max_thought_no_change_streak"),
            {"productive": 1, "failed": 0, "stagnant": 1},
        )

    def test_simulation_replays_fixed_history_and_compares_behavior(self) -> None:
        history = (
            (1, "think", "stagnant", 120, "2026-08-14T00:00:00+00:00"),
            (2, "think", "stagnant", 110, "2026-08-14T00:31:00+00:00"),
            (3, "think", "failed", 100, "2026-08-14T01:02:00+00:00"),
            (4, "action", "productive", 80, "2026-08-14T01:33:00+00:00"),
            (5, "think", "stagnant", 90, "2026-08-14T02:04:00+00:00"),
            (6, "think", "failed", 100, "2026-08-14T02:35:00+00:00"),
            (7, "research", "stagnant", 240, "2026-08-14T03:06:00+00:00"),
            (8, "think", "stagnant", 90, "2026-08-14T03:37:00+00:00"),
            (9, "action", "failed", 80, "2026-08-14T04:08:00+00:00"),
        )
        for row in history:
            self._insert_outcome(*row)

        first = self.manager._simulate("max_thought_no_change_streak", 3, 2)
        second = self.manager._simulate("max_thought_no_change_streak", 3, 2)
        self.assertEqual(first, second)
        self.assertEqual(first["version"], "self-modification-replay/v1")
        self.assertEqual(first["history_source"], "subject_history")
        self.assertEqual(
            set(first["baseline"]),
            {
                "workflow_counts",
                "budget_units",
                "stagnation_triggers",
                "sleep_triggers",
                "maintenance_runs",
            },
        )
        self.assertEqual(
            set(first["diff"]),
            {"workflow_counts", "budget_units", "stagnation_triggers", "sleep_triggers"},
        )
        self.assertGreater(first["diff"]["sleep_triggers"], 0)
        self.assertTrue(first["diff"]["workflow_counts"])

        budget = self.manager._simulate("max_search_rounds_per_run", 2, 3)
        stagnation = self.manager._simulate("max_project_no_progress_reviews", 3, 2)
        self.assertGreater(budget["diff"]["budget_units"], 0)
        self.assertGreater(stagnation["diff"]["stagnation_triggers"], 0)

        self._insert_outcome(10, "think", "productive", 40, "2026-08-14T04:39:00+00:00")
        changed = self.manager._simulate("max_thought_no_change_streak", 3, 2)
        self.assertNotEqual(first["history_hash"], changed["history_hash"])


if __name__ == "__main__":
    unittest.main()
