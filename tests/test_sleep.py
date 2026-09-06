from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.types import canonical_json, content_hash
from noyra.mind import BeliefStore, GoalCandidate, GoalStore, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
)
from noyra.model.errors import ModelCallStateError
from noyra.sleep import (
    FatigueInputs,
    FatigueTracker,
    PersonalityCandidateInput,
    SleepBeliefRevision,
    SleepEngine,
    SleepGoalRevision,
    SleepIntegrity,
    SleepMemory,
    SleepReflectionPlan,
    SleepRetryBlock,
)
from noyra.sleep.errors import SleepStateConflictError


class SleepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "noyra.sqlite3"
        self.subject_id = "Noyra-sleep-test"
        self.genesis_hash = content_hash({"seed": "sleep-test"})
        self.kernel = SubjectKernel(self.db_path, self.subject_id, self.genesis_hash)
        self.database = self.kernel.database
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-11T00:00:00.000+00:00"
        self.sleep = SleepEngine(self.database, self.subject_id, clock=lambda: self.clock_value)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_fatigue_pressure_modes_and_restoration(self) -> None:
        tracker = FatigueTracker(self.database)
        initial = tracker.ensure(self.subject_id)
        self.assertEqual(initial.mode, "active")
        saving = tracker.assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=0.8,
                cognitive_load=0.8,
                frustration=0.5,
                goal_conflict=0.1,
                staleness=0.1,
            ),
            reason="model budget and long reasoning increased load",
        )
        self.assertEqual(saving.mode, "saving")
        exhausted = tracker.assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=1.0,
                cognitive_load=1.0,
                frustration=1.0,
                goal_conflict=1.0,
                staleness=1.0,
            ),
            reason="hard daily budget exhausted",
        )
        self.assertEqual(exhausted.fatigue, 100)
        self.assertEqual(exhausted.mode, "sleeping")
        should_sleep, reasons = tracker.should_sleep(self.subject_id)
        self.assertTrue(should_sleep)
        self.assertIn("hard_resource_budget_exhausted", reasons)
        restored = tracker.restore_after_sleep(self.subject_id, hours=4, reason="rest completed")
        self.assertEqual(restored.fatigue, 28)
        self.assertEqual(restored.resource_pressure, 1)

    def test_sleep_integrity_rejects_corrupt_fatigue_numbers(self) -> None:
        FatigueTracker(self.database).ensure(self.subject_id)
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM fatigue_states WHERE subject_id = ?", (self.subject_id,)
                ).fetchone()
            )
        for column, value in (("fatigue", "nan"), ("fatigue", "bad"), ("version", 1.5)):
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE fatigue_states SET "{column}" = ? WHERE subject_id = ?',
                        (value, self.subject_id),
                    )
                with self.assertRaises(IntegrityError):
                    SleepIntegrity(self.database).verify(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE fatigue_states SET "{column}" = ? WHERE subject_id = ?',
                        (original[column], self.subject_id),
                    )

    def test_sleep_integrity_rejects_invalid_run_numeric_storage(self) -> None:
        run = self.sleep.start("subject_choice", "audit durable sleep numbers")
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT * FROM sleep_runs WHERE sleep_id = ?", (run.sleep_id,)
                ).fetchone()
            )

        def state_hash(
            *,
            emergency: bool = bool(original["emergency"]),
            pre_sleep_fatigue: float = float(original["pre_sleep_fatigue"]),
            version: int = int(original["version"]),
        ) -> str:
            return SleepEngine._state_hash(
                original["status"],
                original["trigger_type"],
                original["trigger_reason"],
                emergency,
                pre_sleep_fatigue,
                original["wake_after"],
                original["reflection_event_id"],
                original["checkpoint_id"],
                version,
                original["completed_at"],
            )

        cases = (
            ("version", 1.5, original["state_hash"]),
            (
                "version",
                sqlite3.Binary(str(original["version"]).encode("ascii")),
                original["state_hash"],
            ),
            ("emergency", 2, state_hash(emergency=True)),
            ("emergency", sqlite3.Binary(b"\x00"), state_hash(emergency=True)),
            (
                "pre_sleep_fatigue",
                sqlite3.Binary(str(original["pre_sleep_fatigue"]).encode("ascii")),
                original["state_hash"],
            ),
            ("pre_sleep_fatigue", 101.0, state_hash(pre_sleep_fatigue=101.0)),
        )
        for column, value, corrupted_hash in cases:
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE sleep_runs SET "{column}" = ?, state_hash = ? WHERE sleep_id = ?',
                        (value, corrupted_hash, run.sleep_id),
                    )
                with self.assertRaises(IntegrityError):
                    SleepIntegrity(self.database).verify(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE sleep_runs SET "{column}" = ?, state_hash = ? WHERE sleep_id = ?',
                        (original[column], original["state_hash"], run.sleep_id),
                    )

    def test_sleep_integrity_rejects_nonfinite_reflection_plan(self) -> None:
        run = self.sleep.start("subject_choice", "audit nonfinite reflection JSON")
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="The persisted reflection plan must remain strict JSON.",
                facts=("One bounded fact was retained.",),
            ),
        )
        SleepIntegrity(self.database).verify(self.subject_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT reflection_id, plan_json FROM sleep_reflections WHERE sleep_id = ?",
                (run.sleep_id,),
            ).fetchone()
            assert row is not None and isinstance(row["plan_json"], str)
            connection.execute("DROP TRIGGER prevent_sleep_reflection_update")
            connection.execute(
                "UPDATE sleep_reflections SET plan_json = ? WHERE reflection_id = ?",
                (f'{row["plan_json"][:-1]},"extra":NaN}}', row["reflection_id"]),
            )

        with self.assertRaises(IntegrityError):
            SleepIntegrity(self.database).verify(self.subject_id)

    def test_sleep_integrity_rejects_invalid_retry_boundary_numbers(self) -> None:
        old_evidence = EventStore(self.database).append(
            self.subject_id, "retry_boundary_evidence", "test", {"old": True}
        )
        with self.database.connection() as connection:
            old_boundary = int(
                connection.execute(
                    "SELECT rowid FROM events WHERE event_id = ?", (old_evidence.event_id,)
                ).fetchone()[0]
            )
        run = self.sleep.start("subject_choice", "audit retry boundary numbers")
        block_id = "retry-integrity-boundary"
        fields = (None, None, "reader", "https://example.com", "wait for evidence", "active", None)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO retry_blocks(
                    block_id, subject_id, goal_id, strategy_id, tool, target, reason,
                    source_sleep_id, status, release_evidence_event_id,
                    evidence_event_boundary, state_hash, created_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    block_id,
                    self.subject_id,
                    *fields[:5],
                    run.sleep_id,
                    fields[5],
                    fields[6],
                    0,
                    SleepEngine._retry_hash(*fields, 0),
                    self.clock_value,
                ),
            )
        for value, boundary in (
            (0.5, 0),
            (sqlite3.Binary(b"0"), 0),
            (-1, -1),
        ):
            with self.subTest(value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "UPDATE retry_blocks SET evidence_event_boundary = ?, state_hash = ? "
                        "WHERE block_id = ?",
                        (value, SleepEngine._retry_hash(*fields, boundary), block_id),
                    )
                with self.assertRaises(IntegrityError):
                    SleepIntegrity(self.database).verify(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE retry_blocks SET evidence_event_boundary = 0, state_hash = ? "
                        "WHERE block_id = ?",
                        (SleepEngine._retry_hash(*fields, 0), block_id),
                    )
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE retry_blocks SET status = 'released',
                    release_evidence_event_id = ?, evidence_event_boundary = ?,
                    state_hash = ?, released_at = ? WHERE block_id = ?""",
                (
                    old_evidence.event_id,
                    old_boundary,
                    SleepEngine._retry_hash(
                        None,
                        None,
                        "reader",
                        "https://example.com",
                        "wait for evidence",
                        "released",
                        old_evidence.event_id,
                        old_boundary,
                    ),
                    self.clock_value,
                    block_id,
                ),
            )
        with self.assertRaises(IntegrityError):
            SleepIntegrity(self.database).verify(self.subject_id)

        other_subject = "Noyra-sleep-other-subject"
        IdentityStore(self.database).ensure(other_subject, content_hash({"seed": other_subject}))
        foreign_evidence = EventStore(self.database).append(
            other_subject, "retry_boundary_evidence", "test", {"foreign": True}
        )
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE retry_blocks SET release_evidence_event_id = ?,
                    evidence_event_boundary = 0, state_hash = ? WHERE block_id = ?""",
                (
                    foreign_evidence.event_id,
                    SleepEngine._retry_hash(
                        None,
                        None,
                        "reader",
                        "https://example.com",
                        "wait for evidence",
                        "released",
                        foreign_evidence.event_id,
                        0,
                    ),
                    block_id,
                ),
            )
        with self.assertRaises(IntegrityError):
            SleepIntegrity(self.database).verify(self.subject_id)

    def test_sleep_integrity_rejects_invalid_personality_numbers(self) -> None:
        evidence_event = EventStore(self.database).append(
            self.subject_id, "personality_evidence", "test", {"evidence": True}
        )
        run = self.sleep.start("subject_choice", "audit personality candidate numbers")
        candidate_id = "personality-integrity-number"
        evidence = (evidence_event.event_id,)

        def candidate_hash(
            direction: float, confidence: float, evidence_ids: tuple[str, ...] = evidence
        ) -> str:
            return content_hash(
                {
                    "trait": "careful",
                    "direction": direction,
                    "confidence": confidence,
                    "evidence_ids": list(evidence_ids),
                    "status": "candidate",
                }
            )

        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO personality_candidates(
                    candidate_id, subject_id, trait, direction, confidence,
                    evidence_ids_json, source_sleep_id, status, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    self.subject_id,
                    "careful",
                    0.5,
                    0.5,
                    canonical_json(list(evidence)),
                    run.sleep_id,
                    "candidate",
                    candidate_hash(0.5, 0.5),
                    self.clock_value,
                ),
            )
            connection.execute("DROP TRIGGER prevent_personality_candidate_update")
        cases = (
            ("direction", sqlite3.Binary(b"0.5"), candidate_hash(0.5, 0.5)),
            ("direction", "nan", candidate_hash(0.5, 0.5)),
            ("direction", 2.0, candidate_hash(2.0, 0.5)),
            ("confidence", sqlite3.Binary(b"0.5"), candidate_hash(0.5, 0.5)),
            ("confidence", "nan", candidate_hash(0.5, 0.5)),
            ("confidence", 2.0, candidate_hash(0.5, 2.0)),
        )
        for column, value, corrupted_hash in cases:
            with self.subTest(column=column, value=value):
                with self.database.transaction() as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f'UPDATE personality_candidates SET "{column}" = ?, state_hash = ? '
                        "WHERE candidate_id = ?",
                        (value, corrupted_hash, candidate_id),
                    )
                with self.assertRaises(IntegrityError):
                    SleepIntegrity(self.database).verify(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE personality_candidates SET "{column}" = 0.5, state_hash = ? '
                        "WHERE candidate_id = ?",
                        (candidate_hash(0.5, 0.5), candidate_id),
                    )
        other_subject = "Noyra-personality-other-subject"
        IdentityStore(self.database).ensure(other_subject, content_hash({"seed": other_subject}))
        foreign_event = EventStore(self.database).append(
            other_subject, "personality_evidence", "test", {"foreign": True}
        )
        for evidence_ids in (("missing-personality-event",), (foreign_event.event_id,)):
            with self.subTest(evidence_ids=evidence_ids):
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE personality_candidates SET evidence_ids_json = ?, state_hash = ? "
                        "WHERE candidate_id = ?",
                        (
                            canonical_json(list(evidence_ids)),
                            candidate_hash(0.5, 0.5, evidence_ids),
                            candidate_id,
                        ),
                    )
                with self.assertRaises(IntegrityError):
                    SleepIntegrity(self.database).verify(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE personality_candidates SET evidence_ids_json = ?, state_hash = ? "
                        "WHERE candidate_id = ?",
                        (
                            canonical_json(list(evidence)),
                            candidate_hash(0.5, 0.5),
                            candidate_id,
                        ),
                    )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE personality_candidates SET evidence_ids_json = ? WHERE candidate_id = ?",
                (sqlite3.Binary(canonical_json(list(evidence)).encode("utf-8")), candidate_id),
            )
        with self.assertRaises(IntegrityError):
            SleepIntegrity(self.database).verify(self.subject_id)

    def test_full_sleep_cycle_commits_checkpoint_and_preserves_identity(self) -> None:
        EventStore(self.database).append(
            self.subject_id, "test_event", "test", {"content": "A day of observation."}
        )
        run = self.sleep.start(
            "subject_choice",
            "I want to integrate the day's experiences",
            wake_after="2026-08-11T02:00:00+00:00",
        )
        self.assertEqual(run.status, "winding_down")
        self.assertEqual(self.kernel.lifecycle.current().state, "winding_down")
        run = self.sleep.begin_reflection(run.sleep_id)
        plan = SleepReflectionPlan(
            summary="I reviewed the day and found one observation worth retaining.",
            facts=("An event was recorded.",),
        )
        run = self.sleep.commit_reflection(run.sleep_id, plan)
        self.assertEqual(run.status, "reflective_sleep")
        self.assertIsNotNone(run.reflection_event_id)
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE sleep_reflections SET summary = 'tampered' WHERE sleep_id = ?",
                (run.sleep_id,),
            )
        run = self.sleep.enter_deep_sleep(run.sleep_id)
        self.assertEqual(run.status, "deep_sleep")
        self.assertEqual(self.kernel.lifecycle.current().state, "deep_sleep")
        identity_before = IdentityStore(self.database).load(self.subject_id)
        self.assertEqual(identity_before.state_version, 1)

        gateway = ModelGateway(
            FakeProvider([]),
            ModelLedger(self.database),
            model="test",
            limits=BudgetLimits(10, 10_000, 10_000, 1_000_000),
            pricing=ModelPricing(),
        )
        with self.assertRaises(ModelCallStateError):
            asyncio.run(
                gateway.complete_structured(
                    self.subject_id,
                    "sleep-check",
                    [ModelMessage(role="user", content="wake")],
                    SleepReflectionPlan,
                    idempotency_key="sleep-check",
                )
            )
        with self.assertRaises(InvalidTransitionError):
            self.kernel.action_ledger.prepare(
                self.subject_id,
                "observe",
                "test-tool",
                "https://example.com",
                {"query": "during sleep"},
            )

        self.clock_value = "2026-08-11T02:00:00.000+00:00"
        run = self.sleep.wake(run.sleep_id, "scheduled wake")
        self.assertEqual(run.status, "waking")
        run = self.sleep.complete_wake(run.sleep_id)
        self.assertEqual(run.status, "complete")
        self.assertEqual(self.kernel.lifecycle.current().state, "active")
        identity_after = IdentityStore(self.database).load(self.subject_id)
        self.assertEqual(identity_after.subject_id, identity_before.subject_id)
        self.assertEqual(identity_after.genesis_hash, identity_before.genesis_hash)
        counts = SleepIntegrity(self.database).verify(self.subject_id)
        self.assertEqual(counts["sleep_runs"], 1)

    def test_sleep_rejects_open_actions_and_enforces_wake_time(self) -> None:
        action = self.kernel.action_ledger.prepare(
            self.subject_id,
            "publish",
            "test-tool",
            "https://example.com",
            {"body": "pending"},
            side_effect=True,
        )
        with self.assertRaises(SleepStateConflictError):
            self.sleep.start("subject_choice", "need rest")
        self.kernel.action_ledger.cancel(action.action_id, "cancelled before sleep")
        run = self.sleep.start(
            "subject_choice",
            "need rest",
            wake_after="2026-08-11T05:00:00+00:00",
        )
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id, SleepReflectionPlan(summary="Nothing requires a public diary.")
        )
        self.sleep.enter_deep_sleep(run.sleep_id)
        with self.assertRaises(SleepStateConflictError):
            self.sleep.wake(run.sleep_id, "too early")
        self.sleep.wake(run.sleep_id, "operator approved early wake", force=True)

    def test_budget_sleep_clears_stale_resource_pressure_only_after_utc_day_rollover(
        self,
    ) -> None:
        tracker = FatigueTracker(self.database)
        tracker.assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=1.0,
                cognitive_load=0.0,
                frustration=0.0,
                goal_conflict=0.0,
                staleness=0.0,
            ),
            reason="daily model budget exhausted",
        )
        run = self.sleep.start(
            "budget",
            "daily model budget exhausted",
            wake_after="2026-08-12T00:00:00+00:00",
        )
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(summary="Wait for the next UTC budget day."),
        )
        self.sleep.enter_deep_sleep(run.sleep_id)
        self.clock_value = "2026-08-12T00:00:00.000+00:00"
        self.sleep.wake(run.sleep_id, "new UTC budget day")
        self.sleep.complete_wake(run.sleep_id)
        restored = tracker.get(self.subject_id)
        self.assertEqual(restored.resource_pressure, 0)
        self.assertEqual(restored.mode, "active")

    def test_restart_during_deep_sleep_preserves_subject_and_sleep_state(self) -> None:
        run = self.sleep.start("subject_choice", "reflect before restart")
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id, SleepReflectionPlan(summary="Reflection survived restart.")
        )
        self.sleep.enter_deep_sleep(run.sleep_id)
        original_identity = IdentityStore(self.database).load(self.subject_id)
        self.kernel.close()
        self.kernel = SubjectKernel(self.db_path, self.subject_id, self.genesis_hash)
        recovered = self.kernel.boot()
        self.assertEqual(recovered.state, "deep_sleep")
        current_sleep = self.sleep.current()
        self.assertIsNotNone(current_sleep)
        assert current_sleep is not None
        self.assertEqual(current_sleep.sleep_id, run.sleep_id)
        reopened_identity = IdentityStore(self.database).load(self.subject_id)
        self.assertEqual(reopened_identity.subject_id, original_identity.subject_id)
        self.assertEqual(reopened_identity.genesis_hash, original_identity.genesis_hash)
        self.assertEqual(reopened_identity.last_checkpoint, original_identity.last_checkpoint)

    def test_invalid_reflection_rolls_back_atomically(self) -> None:
        run = self.sleep.start("subject_choice", "test atomic integration")
        self.sleep.begin_reflection(run.sleep_id)
        with self.assertRaises(NotFoundError):
            self.sleep.commit_reflection(
                run.sleep_id,
                SleepReflectionPlan(
                    summary="This plan contains a nonexistent goal.",
                    goal_revisions=(
                        SleepGoalRevision(
                            goal_id="goal_missing",
                            status="paused",
                            priority=0.5,
                            commitment=0.5,
                            progress=0.1,
                            emotional_pressure=0.0,
                            reason="invalid test",
                        ),
                    ),
                ),
            )
        with self.database.connection() as connection:
            reflection_count = connection.execute(
                "SELECT COUNT(*) FROM sleep_reflections WHERE sleep_id = ?", (run.sleep_id,)
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'sleep_reflection'"
            ).fetchone()[0]
        self.assertEqual(reflection_count, 0)
        self.assertEqual(event_count, 0)
        self.assertIsNone(self.sleep.get(run.sleep_id).reflection_event_id)

    def test_retry_block_requires_new_evidence_before_same_strategy(self) -> None:
        setup_evidence = EventStore(self.database).append(
            self.subject_id, "goal_setup_evidence", "test", {"setup": True}
        )
        retry_goal = GoalStore(self.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Retry evidence goal",
                description="Track whether retry evidence is genuinely new.",
                origin="self",
                priority=0.5,
                commitment=0.5,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(setup_evidence.event_id,),
            reason="test setup",
        )
        action_ids: list[str] = []
        for attempt in range(2):
            action = self.kernel.action_ledger.prepare(
                self.subject_id,
                "observe",
                "web-reader",
                "https://example.com/retry",
                {"attempt": attempt},
                goal_id=retry_goal.goal_id,
                strategy_id="strategy-a",
                idempotency_key=f"retry-{attempt}",
            )
            action = self.kernel.action_ledger.start(action.action_id)
            action = self.kernel.action_ledger.finish(
                action.action_id,
                "failed",
                {"error": "same failure"},
                public_explanation="Observation failed.",
            )
            action_ids.append(action.action_id)
        old_evidence = EventStore(self.database).append(
            self.subject_id, "old_evidence", "test", {"before": True}
        )
        run = self.sleep.start("failures", "same strategy failed twice")
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="The repeated strategy should stop until new evidence appears.",
                retry_blocks=(
                    SleepRetryBlock(
                        tool="web-reader",
                        target="https://example.com/retry",
                        goal_id=retry_goal.goal_id,
                        strategy_id="strategy-a",
                        reason="two attempts failed without new information",
                        action_ids=tuple(action_ids),
                    ),
                ),
            ),
        )
        self.sleep.enter_deep_sleep(run.sleep_id)
        self.sleep.wake(run.sleep_id, "test wake", force=True)
        self.sleep.complete_wake(run.sleep_id)
        with self.database.connection() as connection:
            block_id = connection.execute(
                "SELECT block_id FROM retry_blocks WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        with self.assertRaises(InvalidTransitionError):
            self.kernel.action_ledger.prepare(
                self.subject_id,
                "observe",
                "web-reader",
                "https://example.com/retry",
                {"attempt": 3},
                goal_id=retry_goal.goal_id,
                strategy_id="strategy-a",
                idempotency_key="retry-3",
            )
        with self.assertRaises(SleepStateConflictError):
            self.sleep.release_retry_block(block_id, old_evidence.event_id)
        new_evidence = EventStore(self.database).append(
            self.subject_id, "new_evidence", "test", {"after": True}
        )
        self.sleep.release_retry_block(block_id, new_evidence.event_id)
        prepared = self.kernel.action_ledger.prepare(
            self.subject_id,
            "observe",
            "web-reader",
            "https://example.com/retry",
            {"attempt": 4, "new_evidence": new_evidence.event_id},
            goal_id=retry_goal.goal_id,
            strategy_id="strategy-a",
            idempotency_key="retry-4",
        )
        self.assertEqual(prepared.status, "prepared")
        SleepIntegrity(self.database).verify(self.subject_id)

    def test_reflection_integrates_memory_goal_belief_and_personality_candidate(self) -> None:
        events = EventStore(self.database)
        evidence = tuple(
            events.append(
                self.subject_id,
                "experience",
                "test",
                {"index": index},
            ).event_id
            for index in range(3)
        )
        goal = GoalStore(self.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand the pattern",
                description="Compare independent observations.",
                origin="self",
                priority=0.7,
                commitment=0.5,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(evidence[0],),
            reason="initial curiosity",
        )
        belief = BeliefStore(self.database).create(
            self.subject_id,
            "The pattern may recur.",
            confidence=0.5,
            scope="the observed source",
            supporting_event_ids=(evidence[0],),
        )
        run = self.sleep.start("subject_choice", "integrate structured reflection")
        self.sleep.begin_reflection(run.sleep_id)
        self.sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="The observations were consistent enough to refine my state.",
                facts=("Three experiences were recorded.",),
                memories=(
                    SleepMemory(
                        content="I encountered the same pattern across three observations.",
                        memory_type="reflection",
                        salience=0.8,
                        confidence=0.7,
                        source_event_ids=(evidence[0], evidence[1]),
                    ),
                ),
                goal_revisions=(
                    SleepGoalRevision(
                        goal_id=goal.goal_id,
                        status="active",
                        priority=0.8,
                        commitment=0.6,
                        progress=0.1,
                        emotional_pressure=0.0,
                        reason="sleep review found the question worth pursuing",
                    ),
                ),
                belief_revisions=(
                    SleepBeliefRevision(
                        belief_id=belief.belief_id,
                        proposition="The pattern may recur across independent observations.",
                        confidence=0.65,
                        status="qualified",
                        counter_event_ids=(evidence[2],),
                        reason="sleep review narrowed the belief's scope",
                    ),
                ),
                personality_candidates=(
                    PersonalityCandidateInput(
                        trait="careful curiosity",
                        direction=0.7,
                        confidence=0.4,
                        evidence_ids=evidence,
                    ),
                ),
            ),
        )
        self.assertEqual(GoalStore(self.database).get(goal.goal_id).status, "active")
        self.assertEqual(BeliefStore(self.database).get(belief.belief_id).status, "qualified")
        self.assertEqual(len(MemoryStore(self.database).search(self.subject_id)), 1)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM personality_candidates WHERE subject_id = ?",
                    (self.subject_id,),
                ).fetchone()[0],
                1,
            )
        self.assertEqual(SleepIntegrity(self.database).verify(self.subject_id)["sleep_runs"], 1)

    def test_schema_migrates_from_version_four(self) -> None:
        legacy = Path(self.temp_dir.name) / "legacy-v4.sqlite3"
        raw = sqlite3.connect(legacy)
        raw.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO schema_meta VALUES ('schema_version', '4')")
        raw.commit()
        raw.close()
        migrated = Database(legacy)
        with migrated.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertTrue({"fatigue_states", "sleep_runs", "sleep_reflections"}.issubset(tables))

    def test_plan_validation_rejects_blank_content(self) -> None:
        with self.assertRaises(ValidationError):
            SleepReflectionPlan(summary=" ")
        with self.assertRaises(ValidationError):
            SleepReflectionPlan(summary="valid", public_diary_candidate=" ")
        with self.assertRaises(ValidationError):
            PersonalityCandidateInput(
                trait="repeated evidence",
                direction=0.5,
                confidence=0.5,
                evidence_ids=("evt_same", "evt_same", "evt_same"),
            )
        with self.assertRaises(ValidationError):
            SleepRetryBlock(
                tool="web_read",
                target="https://example.com",
                reason="one attempt is not a repeated failure",
                action_ids=("act_same", "act_same"),
            )


if __name__ == "__main__":
    unittest.main()
