from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.cognition import ActionDeliberation, ActionDeliberationProposal, CognitionSettings
from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.mind import GoalCandidate, GoalStore
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
from noyra.world import SafeWebReader, SourceRegistry


class ActionDeliberationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-action-deliberation-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "action-deliberation-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.evidence = EventStore(self.kernel.database).append(
            self.subject_id,
            "world_evidence",
            "world",
            {"observation_hash": content_hash("a public change worth following")},
        )
        goals = GoalStore(self.kernel.database)
        candidate = goals.create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand a public change",
                description="Gather independent public evidence about the change.",
                origin="self",
                priority=0.75,
                commitment=0.7,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.evidence.event_id,),
            reason="autonomous curiosity",
        )
        self.goal = goals.activate(
            candidate.goal_id,
            rationale="focus on the public change",
            causal_source_ids=(self.evidence.event_id,),
        )
        self.human_goal = goals.create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Do human-proposed work",
                description="This proposal must not drive autonomous action.",
                origin="human_proposal",
                priority=1,
                commitment=1,
                motive_emotion="compliance",
            ),
            causal_source_ids=(self.evidence.event_id,),
            reason="test human proposal isolation",
        )
        self.source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Example Report",
            "https://example.com/report",
            "news",
            status="active",
            reason="authorized test source",
        )
        self.grant = CapabilityStore(self.kernel.database).grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="web_read",
                scope={"hosts": ["example.com"]},
                issuer="operator",
                rate_limit_per_hour=20,
                side_effect=False,
            ),
            actor="operator",
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._web_response))
        self.reader = SafeWebReader(
            client=self.client,
            resolver=self._public_resolver,
            verify_peer_address=False,
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_parser_rejects_blob_durable_json(self) -> None:
        with self.assertRaises(IntegrityError):
            ActionDeliberation._from_row(
                {"deliberation_id": "deliberation-corrupt", "proposal_json": b"{}"}
            )

    @staticmethod
    async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("93.184.216.34",)

    @staticmethod
    async def _web_response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="A fresh public report gives independently checkable evidence.",
        )

    @staticmethod
    def settings(*, max_calls: int = 4, max_goal_actions: int = 3) -> CognitionSettings:
        return CognitionSettings(
            enabled=False,
            action_deliberation_interval_seconds=60,
            max_action_deliberation_model_calls_per_day=max_calls,
            max_action_deliberation_context_chars=24_000,
            max_web_actions_per_goal_per_day=max_goal_actions,
        )

    def deliberation(
        self,
        provider: FakeProvider,
        *,
        max_calls: int = 4,
        max_goal_actions: int = 3,
        limits: BudgetLimits | None = None,
    ) -> ActionDeliberation:
        gateway = ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="test-model",
            limits=limits or BudgetLimits(20, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        return ActionDeliberation(
            self.kernel.database,
            self.subject_id,
            gateway,
            self.settings(max_calls=max_calls, max_goal_actions=max_goal_actions),
            self.reader,
        )

    def proposal(self, *, goal_id: str | None = None, source_id: str | None = None) -> str:
        return json.dumps(
            {
                "summary": "One read-only source can provide relevant independent evidence.",
                "disposition": "investigate",
                "goal_id": goal_id or self.goal.goal_id,
                "source_id": source_id or self.source.source_id,
                "strategy_title": "Read an independent public report",
                "expected_observation": "A report with evidence relevant to the active goal.",
                "reason": "The source is authorized and directly relevant to the goal.",
                "evidence_event_ids": [self.evidence.event_id],
            }
        )

    async def test_successful_model_call_is_reused_after_action_dispatch_crash(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))]
        )
        first = self.deliberation(provider)
        with (
            patch.object(first, "_execute", side_effect=RuntimeError("simulated crash")),
            self.assertRaises(RuntimeError),
        ):
            await first.run_due()
        recovered = self.deliberation(provider)
        self.assertEqual(await recovered.run_due(), "action_deliberation_observed")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(recovered.verify_integrity(), 1)

    async def test_recovery_does_not_repeat_a_completed_read_after_record_crash(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))]
        )
        first = self.deliberation(provider)
        with (
            patch.object(first, "_commit_record", side_effect=RuntimeError("simulated crash")),
            self.assertRaises(RuntimeError),
        ):
            await first.run_due()
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE status = 'succeeded'"
                ).fetchone()[0],
                1,
            )
        recovered = self.deliberation(provider)
        self.assertEqual(await recovered.run_due(), "action_deliberation_unchanged")
        with self.kernel.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(recovered.verify_integrity(), 1)

    async def test_active_goal_drives_one_audited_read_only_action(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))]
        )
        deliberation = self.deliberation(provider)
        self.assertEqual(await deliberation.run_due(), "action_deliberation_observed")
        record = deliberation.latest()
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.goal_id, self.goal.goal_id)
        self.assertEqual(record.source_id, self.source.source_id)
        self.assertIsNotNone(record.action_id)
        self.assertIsNotNone(record.observation_id)
        self.assertEqual(deliberation.verify_integrity(), 1)
        with self.kernel.database.connection() as connection:
            action = connection.execute(
                "SELECT goal_id, strategy_id, side_effect, status FROM actions WHERE action_id = ?",
                (record.action_id,),
            ).fetchone()
            log = connection.execute(
                "SELECT public_goal_reference, public_target, result_status "
                "FROM behavior_logs WHERE action_id = ?",
                (record.action_id,),
            ).fetchone()
            outgoing = connection.execute(
                "SELECT COUNT(*) FROM interactions WHERE direction = 'outgoing'"
            ).fetchone()[0]
        self.assertEqual(action["goal_id"], self.goal.goal_id)
        self.assertTrue(action["strategy_id"])
        self.assertFalse(action["side_effect"])
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(log["public_goal_reference"], self.goal.goal_id)
        self.assertEqual(log["public_target"], self.source.url)
        self.assertEqual(log["result_status"], "succeeded")
        self.assertEqual(outgoing, 0)
        system, context = provider.requests[0].messages
        self.assertIn("read-only evidence-gathering", system.content)
        self.assertNotIn(self.human_goal.goal_id, context.content)

    async def test_subject_can_deliberately_wait_without_creating_an_action(self) -> None:
        payload = {
            "summary": "No available read is presently worth the resource cost.",
            "disposition": "wait",
            "goal_id": None,
            "source_id": None,
            "strategy_title": None,
            "expected_observation": None,
            "reason": "The current source was observed recently and no new signal is expected.",
            "evidence_event_ids": [self.evidence.event_id],
        }
        deliberation = self.deliberation(
            FakeProvider(
                [ProviderResponse(content=json.dumps(payload), usage=ModelUsage(500, 180))]
            )
        )
        self.assertEqual(await deliberation.run_due(), "action_deliberation_waited")
        record = deliberation.latest()
        assert record is not None
        self.assertEqual(record.status, "waited")
        self.assertIsNone(record.goal_id)
        self.assertIsNone(record.action_id)
        with self.kernel.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    async def test_forged_goal_or_source_is_rejected_without_tool_execution(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.proposal(goal_id="goal_forged"),
                    usage=ModelUsage(500, 180),
                ),
                ProviderResponse(
                    content=self.proposal(source_id="src_forged"),
                    usage=ModelUsage(500, 180),
                ),
            ]
        )
        deliberation = self.deliberation(provider)
        self.assertEqual(await deliberation.run_due(), "action_deliberation_rejected")
        self.assertEqual(await deliberation.run_due(), "action_deliberation_rejected")
        with self.kernel.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM action_deliberation_runs").fetchone()[0],
                0,
            )

    async def test_revoked_capability_blocks_deliberation_before_model_call(self) -> None:
        CapabilityStore(self.kernel.database).revoke(
            self.grant.grant_id,
            reason="operator withdrew access",
            actor="operator",
            subject_id=self.subject_id,
        )
        provider = FakeProvider([])
        self.assertIsNone(await self.deliberation(provider).run_due())
        self.assertEqual(provider.requests, [])

    async def test_daily_call_budget_and_hard_budget_are_bounded(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.proposal(goal_id="goal_forged"),
                    usage=ModelUsage(500, 180),
                )
            ]
        )
        capped = self.deliberation(provider, max_calls=1)
        self.assertEqual(await capped.run_due(), "action_deliberation_rejected")
        self.assertIsNone(await capped.run_due())
        exhausted = self.deliberation(
            FakeProvider([]),
            limits=BudgetLimits(0, 500_000, 100_000, 5_000_000),
        )
        self.assertEqual(await exhausted.run_due(), "action_deliberation_budget_exhausted")
        self.assertEqual(exhausted.fatigue.get(self.subject_id).resource_pressure, 1)

    async def test_per_goal_daily_action_limit_prevents_repeated_read(self) -> None:
        first = self.deliberation(
            FakeProvider([ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))]),
            max_goal_actions=1,
        )
        self.assertEqual(await first.run_due(), "action_deliberation_observed")
        second = self.deliberation(
            FakeProvider([ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))]),
            max_goal_actions=1,
        )
        context = second._context(second._active_goals(), second._authorized_sources())
        with self.assertRaisesRegex(ValueError, "goal action limit reached"):
            second._validate(
                ActionDeliberationProposal.model_validate_json(self.proposal()),
                context,
                second.clock()[:10],
                "call_test",
            )

    async def test_records_are_append_only(self) -> None:
        deliberation = self.deliberation(
            FakeProvider([ProviderResponse(content=self.proposal(), usage=ModelUsage(650, 220))])
        )
        self.assertEqual(await deliberation.run_due(), "action_deliberation_observed")
        record = deliberation.latest()
        assert record is not None
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE action_deliberation_runs SET status = 'failed' WHERE deliberation_id = ?",
                (record.deliberation_id,),
            )


if __name__ == "__main__":
    unittest.main()
