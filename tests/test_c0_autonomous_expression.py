from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from noyra.cognition import CognitionSettings
from noyra.cognition.expression import AutonomousExpression
from noyra.core import SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.interaction.public_archive import PublicArchive
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
from noyra.model.types import CompletionRequest


class Writer:
    name = "c0-writer-fixture"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.operation = "write"
        self.approved = True

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        self.requests.append(request)
        output: dict[str, Any]
        if request.schema_name == "ExpressionProposal":
            output = {
                "operation": self.operation,
                "title": "What makes a question interesting?",
                "summary": "I found a question I want to keep exploring.",
                "body": "I would like to compare different accounts and revise my view.",
                "reason": "A subject-chosen expression",
            }
            if self.operation == "wait":
                output = {
                    "operation": "wait",
                    "reason": "I prefer to wait for more useful experience.",
                }
        else:
            output = {
                "approved": self.approved,
                "reason": "This text is suitable for public sharing.",
            }
        return ProviderResponse(json.dumps(output), ModelUsage(100, 80))


class AutonomousExpressionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kernel = SubjectKernel(
            self.root / "subject.sqlite3", "c0-writer", content_hash("writer")
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.subject = self.kernel.subject_id
        self.db = self.kernel.database
        self.provider = Writer()
        self.now = datetime.now(UTC)
        self.settings = CognitionSettings(enabled=True, expression_interval_seconds=300)
        self.gateway = ModelGateway(
            self.provider,
            ModelLedger(self.db),
            model="fixture",
            limits=BudgetLimits(100, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.worker = AutonomousExpression(
            self.db, self.subject, self.gateway, self.settings, clock=lambda: self.now.isoformat()
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    async def test_draft_self_review_publish_then_interval_wait_without_human_task(self) -> None:
        self.assertEqual(await self.worker.run_due(), "expression_published")
        self.assertEqual(
            [request.schema_name for request in self.provider.requests],
            ["ExpressionProposal", "ExpressionReview"],
        )
        self.assertIsNone(await self.worker.run_due())
        self.assertEqual(len(self.provider.requests), 2)
        items = PublicArchive(self.db).admin(self.subject)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["provenance"], "subject")
        ActionLedger(self.db).verify_integrity(self.subject)

    async def test_wait_is_durable_and_does_not_create_a_side_effect(self) -> None:
        self.provider.operation = "wait"
        self.assertEqual(await self.worker.run_due(), "expression_wait")
        self.assertIsNone(await self.worker.run_due())
        self.assertEqual(len(self.provider.requests), 1)
        with self.db.connection() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    async def test_declined_self_review_keeps_draft_private_and_stops(self) -> None:
        self.provider.approved = False
        self.assertEqual(await self.worker.run_due(), "expression_declined")
        self.assertIsNone(await self.worker.run_due())
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])
        self.assertEqual(len(self.provider.requests), 2)

    async def test_review_resumes_after_budget_day_without_repeating_planner(self) -> None:
        self.worker.settings = self.settings.model_copy(
            update={"max_expression_model_calls_per_day": 1}
        )
        self.assertEqual(await self.worker.run_due(), "expression_budget_exhausted")
        self.assertEqual(len(self.provider.requests), 1)
        self.now += timedelta(days=1)
        self.assertEqual(await self.worker.run_due(), "expression_published")
        self.assertEqual(len(self.provider.requests), 2)

    async def test_terminal_receipt_recovers_without_duplicate_publication(self) -> None:
        with (
            patch.object(self.worker, "_terminal", side_effect=RuntimeError("interrupted")),
            self.assertRaises(RuntimeError),
        ):
            await self.worker.run_due()
        self.assertEqual(len(PublicArchive(self.db).admin(self.subject)), 1)
        self.assertEqual(await self.worker.run_due(), "expression_recovered")
        self.assertEqual(len(PublicArchive(self.db).admin(self.subject)), 1)
        self.assertEqual(len(self.provider.requests), 2)

    async def test_unknown_review_is_not_resent_on_next_day(self) -> None:
        self.worker.settings = self.settings.model_copy(
            update={"max_expression_model_calls_per_day": 1}
        )
        await self.worker.run_due()
        with self.db.connection() as c:
            action_id = str(c.execute("SELECT action_id FROM actions").fetchone()[0])
        purpose = "expression_review:" + action_id
        ledger = ModelLedger(self.db)
        call, _ = ledger.prepare_call(
            self.subject, self.provider.name, "fixture", purpose, content_hash("review"), purpose
        )
        attempt = ledger.authorize_attempt(
            call.call_id,
            BudgetLimits(100, 500_000, 100_000, 5_000_000),
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_cost_microusd=0,
        )
        ledger.start_attempt(attempt.attempt_id)
        ledger.finish_attempt(
            attempt.attempt_id, "unknown", usage=None, cost_microusd=None, error_code="unknown"
        )
        ledger.finish_call(call.call_id, "unknown", error_code="unknown")
        self.now += timedelta(days=1)
        self.assertEqual(await self.worker.run_due(), "expression_review_wait")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    def test_public_expression_context_excludes_private_memories_goals_and_messages(self) -> None:
        from noyra.core import EventStore
        from noyra.interaction import InteractionStore

        InteractionStore(self.db).receive(
            self.subject, "web", "web-user", "PRIVATE-MESSAGE-SENTINEL"
        )
        EventStore(self.db).append(
            self.subject, "private_experience", "subject", {"private": "PRIVATE-SENTINEL"}
        )
        self.assertNotIn("PRIVATE", json.dumps(self.worker._context()))

    async def test_real_cycle_expression_lane_publishes_and_rotates_other_internal_work(
        self,
    ) -> None:
        from noyra.cognition import CognitionCycle
        from noyra.core.fair_scheduling import WorkflowOutcome

        cycle = CognitionCycle(
            self.kernel, self.gateway, self.settings, clock=lambda: self.now.isoformat()
        )
        try:
            cycle.bootstrap()
            with patch.object(cycle.scheduler, "next_target", return_value="expression"):
                result = await cycle._dispatch_workflow("metacognition")
            self.assertIsInstance(result, WorkflowOutcome)
            assert isinstance(result, WorkflowOutcome)
            self.assertEqual(result.reason, "expression_published")
            self.assertTrue(result.made_progress)
            self.assertEqual(result.target_id, "expression")
            for previous, expected in (
                ("attention", "expression"),
                ("expression", "help"),
                ("help", "attention"),
            ):
                cycle.scheduler._current = {"workflows": {"metacognition": {"target_id": previous}}}
                self.assertEqual(
                    cycle.scheduler.next_target(
                        "metacognition", ["attention", "expression", "help"]
                    ),
                    expected,
                )
        finally:
            await cycle.aclose()

    async def test_known_failed_planner_settles_and_can_be_reconsidered_after_interval(
        self,
    ) -> None:
        from noyra.model.errors import ProviderCallError

        provider = FakeProvider(
            [ProviderCallError("provider_http_503", retryable=False, outcome_unknown=False)]
        )
        self.worker.gateway = ModelGateway(
            provider,
            ModelLedger(self.db),
            model="fixture",
            limits=BudgetLimits(100, 500_000, 100_000, 5_000_000),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.assertEqual(await self.worker.run_due(), "expression_model_wait")
        self.assertEqual(await self.worker.run_due(), "expression_failed")
        self.assertIsNone(await self.worker.run_due())
        self.assertEqual(len(provider.requests), 1)
        self.worker.gateway = self.gateway
        self.now += timedelta(seconds=301)
        self.assertEqual(await self.worker.run_due(), "expression_published")
        self.assertEqual(len(self.provider.requests), 2)
