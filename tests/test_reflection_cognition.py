from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from noyra.cognition import (
    CognitionSettings,
    ReflectionCognitionPending,
    ReflectionCognitionValidationError,
    SleepReflectionCognition,
)
from noyra.core import EventStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.mind import BeliefStore, GoalCandidate, GoalStore, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)
from noyra.sleep import (
    PersonalityCandidateInput,
    SleepBeliefRevision,
    SleepEngine,
    SleepGoalRevision,
    SleepReflectionPlan,
    SleepRetryBlock,
)


class ReflectionCognitionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-reflection-cognition-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "reflection-cognition-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.events = EventStore(self.kernel.database)
        self.evidence = tuple(
            self.events.append(
                self.subject_id,
                "experience",
                "test",
                {"index": index, "private_value": f"must-not-leak-{index}"},
            ).event_id
            for index in range(3)
        )
        self.goal = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand a recurring pattern",
                description="Compare evidence while preserving uncertainty.",
                origin="self",
                priority=0.7,
                commitment=0.6,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.evidence[0],),
            reason="curiosity emerged from repeated evidence",
        )
        self.belief = BeliefStore(self.kernel.database).create(
            self.subject_id,
            "The pattern may recur.",
            confidence=0.5,
            scope="recent evidence",
            supporting_event_ids=(self.evidence[0],),
        )
        self.failed_action_ids: list[str] = []
        for index in range(2):
            action = self.kernel.action_ledger.prepare(
                self.subject_id,
                "observe",
                "web-reader",
                "https://example.com/retry",
                {"attempt": index},
                goal_id=self.goal.goal_id,
                strategy_id="compare-source",
                idempotency_key=f"reflection-failed-action-{index}",
            )
            self.kernel.action_ledger.start(action.action_id)
            self.kernel.action_ledger.finish(
                action.action_id,
                "failed",
                {"error": "same failure"},
                public_explanation="The bounded observation failed.",
            )
            self.failed_action_ids.append(action.action_id)
        self.sleep = SleepEngine(self.kernel.database, self.subject_id)
        self.sleep_run = self.sleep.start("subject_choice", "integrate recent experience")
        self.sleep_run = self.sleep.begin_reflection(self.sleep_run.sleep_id)

    async def asyncTearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def settings(self, *, max_calls: int = 3, max_active: int = 3) -> CognitionSettings:
        return CognitionSettings(
            enabled=False,
            max_sleep_model_calls_per_run=max_calls,
            max_sleep_context_chars=60_000,
            max_active_goals=max_active,
        )

    def cognition(
        self,
        provider: FakeProvider,
        *,
        max_calls: int = 3,
        max_active: int = 3,
        limits: BudgetLimits | None = None,
    ) -> SleepReflectionCognition:
        gateway = ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="test-model",
            limits=limits or BudgetLimits(20, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        return SleepReflectionCognition(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(max_calls=max_calls, max_active=max_active),
        )

    def proposal(self, *, event_id: str | None = None) -> str:
        source = event_id or self.evidence[0]
        return json.dumps(
            {
                "summary": "Repeated evidence supports a cautious continuation of the inquiry.",
                "facts": ["Three experience events were available before sleep."],
                "memories": [
                    {
                        "content": "I noticed a recurring pattern and retained uncertainty.",
                        "memory_type": "reflection",
                        "salience": 0.75,
                        "confidence": 0.65,
                        "source_event_ids": [source],
                    }
                ],
                "goal_revisions": [
                    {
                        "goal_id": self.goal.goal_id,
                        "status": "active",
                        "priority": 0.75,
                        "commitment": 0.65,
                        "progress": 0.1,
                        "emotional_pressure": 0.1,
                        "reason": "the inquiry remains supported after reflection",
                    }
                ],
                "personality_candidates": [
                    {
                        "trait": "cautious curiosity",
                        "direction": 0.7,
                        "confidence": 0.4,
                        "evidence_ids": list(self.evidence),
                    }
                ],
            }
        )

    async def test_reflection_proposal_is_cached_validated_and_atomically_integrated(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(1_000, 500))]
        )
        cognition = self.cognition(provider)
        plan = await cognition.propose(self.sleep_run)
        recovered = await cognition.propose(self.sleep_run)
        self.assertEqual(recovered, plan)
        self.assertEqual(len(provider.requests), 1)
        system_message, context_message = provider.requests[0].messages
        self.assertIn("untrusted data", system_message.content)
        self.assertIn("BEGIN_UNTRUSTED_REFLECTION_CONTEXT", context_message.content)
        self.assertNotIn("must-not-leak", context_message.content)
        self.assertIn(self.evidence[0], context_message.content)

        self.sleep.commit_reflection(self.sleep_run.sleep_id, plan)
        self.assertEqual(GoalStore(self.kernel.database).get(self.goal.goal_id).status, "active")
        memories = MemoryStore(self.kernel.database).search(self.subject_id)
        self.assertEqual(len(memories), 1)
        self.assertIn("recurring pattern", memories[0].content)
        with self.kernel.database.connection() as connection:
            candidates = connection.execute(
                "SELECT COUNT(*) FROM personality_candidates WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        self.assertEqual(candidates, 1)

    async def test_invalid_causal_reference_falls_back_without_partial_state(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.proposal(event_id="evt_forged"),
                    usage=ModelUsage(800, 400),
                )
            ]
        )
        cognition = self.cognition(provider, max_calls=1)
        plan = await cognition.propose(self.sleep_run)
        self.assertIn("fallback reflection", plan.summary)
        self.assertEqual(plan.memories, ())
        self.assertEqual(plan.goal_revisions, ())
        with self.kernel.database.connection() as connection:
            rejected = connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'sleep_reflection_rejected'"
            ).fetchone()[0]
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
        self.assertEqual(rejected, 1)
        self.sleep.commit_reflection(self.sleep_run.sleep_id, plan)

    async def test_invalid_saved_response_is_skipped_before_a_later_valid_recovery(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.proposal(event_id="evt_forged"),
                    usage=ModelUsage(800, 400),
                ),
                ProviderResponse(content=self.proposal(), usage=ModelUsage(800, 400)),
            ]
        )
        cognition = self.cognition(provider, max_calls=2)
        with self.assertRaises(ReflectionCognitionPending):
            await cognition.propose(self.sleep_run)
        plan = await cognition.propose(self.sleep_run)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(plan.goal_revisions[0].goal_id, self.goal.goal_id)

    async def test_provider_failure_reaches_bounded_fallback(self) -> None:
        provider = FakeProvider([])
        cognition = self.cognition(provider, max_calls=1)
        plan = await cognition.propose(self.sleep_run)
        self.assertIn("remote reflection model unavailable", plan.summary)
        with self.kernel.database.connection() as connection:
            call = connection.execute(
                "SELECT status, purpose FROM model_calls WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()
        self.assertEqual(call["status"], "unknown")
        self.assertEqual(call["purpose"], f"sleep_reflection:{self.sleep_run.sleep_id}")

    async def test_provider_failure_remains_pending_before_call_cap(self) -> None:
        with self.assertRaises(ReflectionCognitionPending):
            await self.cognition(FakeProvider([]), max_calls=2).propose(self.sleep_run)

    async def test_budget_denial_uses_immediate_no_change_fallback(self) -> None:
        budget_plan = await self.cognition(
            FakeProvider([]),
            max_calls=1,
            limits=BudgetLimits(0, 500_000, 100_000, 5_000_000),
        ).propose(self.sleep_run)
        self.assertIn("budget unavailable", budget_plan.summary)

    async def test_supervisor_rejects_invalid_sleep_targets_before_engine_commit(self) -> None:
        cognition = self.cognition(FakeProvider([]))
        context = cognition._context(self.sleep_run)
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="A forged retry barrier must not be committed.",
                    retry_blocks=(
                        SleepRetryBlock(
                            tool="web_read",
                            target="https://example.com/forged",
                            reason="forged failed actions",
                            action_ids=("act_one", "act_two"),
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="A mismatched retry barrier must not be committed.",
                    retry_blocks=(
                        SleepRetryBlock(
                            tool="wrong-tool",
                            target="https://example.com/retry",
                            goal_id=self.goal.goal_id,
                            strategy_id="compare-source",
                            reason="the signature does not match",
                            action_ids=tuple(self.failed_action_ids),
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="A forged goal must not be revised.",
                    goal_revisions=(
                        SleepGoalRevision(
                            goal_id="goal_forged",
                            status="active",
                            priority=0.5,
                            commitment=0.5,
                            progress=0,
                            emotional_pressure=0,
                            reason="forged goal",
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="An invalid goal transition must not be committed.",
                    goal_revisions=(
                        SleepGoalRevision(
                            goal_id=self.goal.goal_id,
                            status="paused",
                            priority=0.5,
                            commitment=0.5,
                            progress=0,
                            emotional_pressure=0,
                            reason="candidate goals cannot become paused",
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="A belief revision needs in-window evidence.",
                    belief_revisions=(
                        SleepBeliefRevision(
                            belief_id=self.belief.belief_id,
                            proposition="The pattern may recur.",
                            confidence=0.5,
                            status="qualified",
                            reason="missing evidence",
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ReflectionCognitionValidationError):
            cognition._validate(
                SleepReflectionPlan(
                    summary="A forged personality candidate must not be committed.",
                    personality_candidates=(
                        PersonalityCandidateInput(
                            trait="forged confidence",
                            direction=0.5,
                            confidence=0.5,
                            evidence_ids=("evt_one", "evt_two", "evt_three"),
                        ),
                    ),
                ),
                context,
            )
        with self.assertRaises(ValueError):
            await cognition.propose(replace(self.sleep_run, status="deep_sleep"))

    async def test_reflection_cannot_bypass_active_goal_limit(self) -> None:
        second = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="A second sleep direction",
                description="This direction must respect the shared active-goal limit.",
                origin="environment",
                priority=0.6,
                commitment=0.5,
                motive_emotion="interest",
            ),
            causal_source_ids=(self.evidence[1],),
            reason="test sleep active-goal limit",
        )
        payload = json.loads(self.proposal())
        payload["goal_revisions"].append(
            {
                "goal_id": second.goal_id,
                "status": "active",
                "priority": 0.65,
                "commitment": 0.55,
                "progress": 0,
                "emotional_pressure": 0,
                "reason": "a second direction also appears relevant",
            }
        )
        provider = FakeProvider(
            [ProviderResponse(content=json.dumps(payload), usage=ModelUsage(900, 400))]
        )
        plan = await self.cognition(provider, max_calls=1, max_active=1).propose(self.sleep_run)
        self.assertIn("fallback reflection", plan.summary)
        self.assertEqual(plan.goal_revisions, ())

    async def test_events_arriving_after_sleep_start_are_not_in_current_context(self) -> None:
        later = self.events.append(
            self.subject_id,
            "late_event",
            "test",
            {"note": "must wait for the next sleep"},
            occurred_at="2099-01-01T00:00:00.000+00:00",
        )
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(1_000, 500))]
        )
        await self.cognition(provider).propose(self.sleep_run)
        _, context_message = provider.requests[0].messages
        self.assertNotIn(later.event_id, context_message.content)


if __name__ == "__main__":
    unittest.main()
