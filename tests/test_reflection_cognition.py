from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Self

from pydantic import ValidationError

from noyra.cognition import (
    CognitionSettings,
    ReflectionCognitionPending,
    ReflectionCognitionValidationError,
    SleepReflectionCognition,
)
from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.mind import BeliefStore, GoalCandidate, GoalStore, MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)
from noyra.model.request_identity import business_request_key
from noyra.sleep import (
    PersonalityCandidateInput,
    SleepBeliefRevision,
    SleepEngine,
    SleepGoalRevision,
    SleepReflectionPlan,
    SleepRetryBlock,
)
from noyra.sleep.types import SleepReflectionProposal


class ReflectionCognitionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self: Self) -> None:
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

    async def asyncTearDown(self: Self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def settings(self: Self, *, max_calls: int = 3) -> CognitionSettings:
        return CognitionSettings(
            enabled=False,
            max_sleep_model_calls_per_run=max_calls,
            max_sleep_context_chars=60_000,
        )

    def cognition(
        self: Self,
        provider: FakeProvider,
        *,
        max_calls: int = 3,
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
            self.settings(max_calls=max_calls),
        )

    def proposal(self: Self, *, event_id: str | None = None) -> str:
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

    async def test_reflection_proposal_is_cached_validated_and_atomically_integrated(
        self: Self,
    ) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(1_000, 500))]
        )
        cognition = self.cognition(provider)
        plan = await cognition.propose(self.sleep_run)
        recovered = await self.cognition(provider).propose(self.sleep_run)
        self.assertEqual(recovered, plan)
        self.assertEqual(len(provider.requests), 1)
        system_message, context_message = provider.requests[0].messages
        self.assertIn("untrusted data", system_message.content)
        self.assertIn("Goals are read-only context", system_message.content)
        self.assertIn(self.goal.goal_id, context_message.content)
        self.assertNotIn("goal_revisions", provider.requests[0].output_schema["properties"])
        self.assertIn("BEGIN_UNTRUSTED_REFLECTION_CONTEXT", context_message.content)
        self.assertNotIn("must-not-leak", context_message.content)
        self.assertIn(self.evidence[0], context_message.content)

        goals = GoalStore(self.kernel.database)
        history = goals.revisions(self.goal.goal_id)
        self.sleep.commit_reflection(self.sleep_run.sleep_id, plan)
        self.assertEqual(goals.get(self.goal.goal_id), self.goal)
        self.assertEqual(goals.revisions(self.goal.goal_id), history)
        memories = MemoryStore(self.kernel.database).search(self.subject_id)
        self.assertEqual(len(memories), 1)
        self.assertIn("recurring pattern", memories[0].content)
        with self.kernel.database.connection() as connection:
            candidates = connection.execute(
                "SELECT COUNT(*) FROM personality_candidates WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        self.assertEqual(candidates, 1)

    async def test_invalid_causal_reference_falls_back_without_partial_state(self: Self) -> None:
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

    async def test_invalid_saved_response_is_skipped_before_a_later_valid_recovery(
        self: Self,
    ) -> None:
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
        self.assertEqual(plan.memories[0].source_event_ids, (self.evidence[0],))

    async def test_provider_failure_reaches_bounded_fallback(self: Self) -> None:
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

    async def test_provider_failure_remains_pending_before_call_cap(self: Self) -> None:
        with self.assertRaises(ReflectionCognitionPending):
            await self.cognition(FakeProvider([]), max_calls=2).propose(self.sleep_run)

    async def test_budget_denial_uses_immediate_no_change_fallback(self: Self) -> None:
        budget_plan = await self.cognition(
            FakeProvider([]),
            max_calls=1,
            limits=BudgetLimits(0, 500_000, 100_000, 5_000_000),
        ).propose(self.sleep_run)
        self.assertIn("budget unavailable", budget_plan.summary)

    async def test_supervisor_rejects_invalid_sleep_targets_before_engine_commit(
        self: Self,
    ) -> None:
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

    def legacy_proposal(self: Self, *, revise_goal: bool) -> SleepReflectionPlan:
        return SleepReflectionPlan.model_validate_json(self.proposal()).model_copy(
            update={
                "goal_revisions": (
                    SleepGoalRevision(
                        goal_id=self.goal.goal_id,
                        status="active",
                        priority=0.75,
                        commitment=0.65,
                        progress=0.1,
                        emotional_pressure=0.1,
                        reason="legacy fixture",
                    ),
                )
                if revise_goal
                else (),
            }
        )

    async def seed_successful_legacy_call(self: Self, plan: SleepReflectionPlan) -> str:
        provider = FakeProvider([ProviderResponse(plan.model_dump_json(), ModelUsage(10, 10))])
        gateway = self.cognition(provider).gateway
        purpose = f"sleep_reflection:{self.sleep_run.sleep_id}"
        result = await gateway.complete_structured(
            self.subject_id,
            purpose,
            (ModelMessage(role="user", content="Legacy fixture"),),
            SleepReflectionPlan,
            idempotency_key=business_request_key(self.kernel.database, self.subject_id, purpose),
        )
        return result.call_id

    async def test_new_model_contract_rejects_goal_revision_fields(self: Self) -> None:
        for revise_goal in (False, True):
            with self.subTest(revise_goal=revise_goal):
                payload = self.legacy_proposal(revise_goal=revise_goal).model_dump_json()
                with self.assertRaises(ValidationError):
                    SleepReflectionProposal.model_validate_json(payload)
        provider = FakeProvider(
            [
                ProviderResponse(
                    self.legacy_proposal(revise_goal=True).model_dump_json(), ModelUsage(10, 10)
                )
            ]
        )
        plan = await self.cognition(provider, max_calls=1).propose(self.sleep_run)
        self.assertIn("fallback reflection", plan.summary)
        self.assertFalse(plan.goal_revisions or plan.memories)
        self.assertEqual(len(provider.requests), 1)

    async def test_legacy_plan_with_goal_revision_falls_back_at_call_limit(self: Self) -> None:
        call_id = await self.seed_successful_legacy_call(self.legacy_proposal(revise_goal=True))
        ledger = ModelLedger(self.kernel.database)
        call = ledger.get_call(call_id)
        provider = FakeProvider([])
        cognition = self.cognition(provider, max_calls=1)
        plan = await cognition.propose(self.sleep_run)
        self.assertEqual(await cognition.propose(self.sleep_run), plan)
        self.assertIn("fallback reflection", plan.summary)
        self.assertFalse(plan.goal_revisions or plan.memories)
        self.assertFalse(provider.requests)
        self.assertEqual(ledger.get_call(call_id), call)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'sleep_reflection_rejected'"
                ).fetchone()[0],
                1,
            )

    async def test_legacy_plan_with_goal_revision_allows_bounded_fresh_candidate(
        self: Self,
    ) -> None:
        await self.seed_successful_legacy_call(self.legacy_proposal(revise_goal=True))
        provider = FakeProvider([ProviderResponse(self.proposal(), ModelUsage(10, 10))])
        plan = await self.cognition(provider, max_calls=2).propose(self.sleep_run)
        self.assertFalse(plan.goal_revisions)
        self.assertTrue(plan.memories)
        self.assertEqual(len(provider.requests), 1)

    async def test_legacy_plan_without_goal_revisions_recovers_without_a_new_call(
        self: Self,
    ) -> None:
        legacy = self.legacy_proposal(revise_goal=False)
        await self.seed_successful_legacy_call(legacy)
        provider = FakeProvider([])
        plan = await self.cognition(provider, max_calls=1).propose(self.sleep_run)
        self.assertEqual(plan, legacy)
        self.assertFalse(provider.requests)

    async def test_cache_integrity_damage_is_not_a_policy_rejection(self: Self) -> None:
        call_id = await self.seed_successful_legacy_call(self.legacy_proposal(revise_goal=False))
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET response_hash=? WHERE call_id=?", ("0" * 64, call_id)
            )
        provider = FakeProvider([])
        with self.assertRaises(IntegrityError):
            await self.cognition(provider).propose(self.sleep_run)
        self.assertFalse(provider.requests)

    async def test_pending_legacy_context_conflict_does_not_send_or_mutate_call(self: Self) -> None:
        purpose = f"sleep_reflection:{self.sleep_run.sleep_id}"
        ledger = ModelLedger(self.kernel.database)
        provider = FakeProvider([])
        call, _ = ledger.prepare_call(
            self.subject_id,
            provider.name,
            "test-model",
            purpose,
            content_hash("old contract"),
            business_request_key(self.kernel.database, self.subject_id, purpose),
        )
        plan = await self.cognition(provider).propose(self.sleep_run)
        self.assertIn("pending reflection request context changed", plan.summary)
        self.assertFalse(provider.requests)
        self.assertEqual(ledger.get_call(call.call_id), call)
        self.assertFalse(ledger.attempts(call.call_id))

    async def test_events_arriving_after_sleep_start_are_not_in_current_context(self: Self) -> None:
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
