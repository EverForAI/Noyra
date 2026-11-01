from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.cognition import (
    AutonomousResearch,
    CognitionCycle,
    CognitionSettings,
    GoalGovernance,
    IntrinsicThought,
    MetacognitiveControl,
)
from noyra.core import EventStore, SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.learning import OutcomeEvaluator
from noyra.mind import GoalStore, MindEngine
from noyra.mind.types import AffectImpulse, AppraisalInput
from noyra.model import (
    BudgetLimits,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)
from noyra.model.types import CompletionRequest
from noyra.research import BrowserSearchExecutor
from noyra.world import SafeWebReader, WorldIntegrity


class ChainProvider:
    name = "b2-chain-fixture"

    def __init__(self, *, propose_goal: bool = True, wait_research: bool = False) -> None:
        self.requests: list[CompletionRequest] = []
        self.propose_goal = propose_goal
        self.wait_research = wait_research

    @staticmethod
    def context(request: CompletionRequest) -> dict[str, Any]:
        text = request.messages[1].content
        if text.startswith('{"format"'):
            value: dict[str, Any] = json.loads(text)["context"]
            return value
        if "DATA> " in text:
            return json.loads(text.split("DATA> ", 1)[1].split("\nEND_", 1)[0])  # type: ignore[no-any-return]
        return json.loads(text)  # type: ignore[no-any-return]

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        self.requests.append(request)
        context = self.context(request)
        proposal: dict[str, Any]
        if request.schema_name == "IntrinsicThoughtProposal":
            goal = None
            if self.propose_goal:
                goal = {
                    "title": "Understand changing environments",
                    "description": (
                        "Explore independent evidence to understand how environments change."
                    ),
                    "priority": 0.6,
                    "commitment": 0.55,
                    "motive_emotion": "curiosity",
                    "motive_target_type": "world",
                    "motive_target_id": None,
                }
            proposal = {
                "disposition": "reflect",
                "summary": "Consider an internally felt question.",
                "insight": "I can examine this uncertainty through independent observations.",
                "next_question": "What evidence would clarify this uncertainty?",
                "source_event_ids": [context["events"][0]["event_id"]],
                "source_memory_ids": [],
                "source_belief_ids": [],
                "source_goal_ids": [],
                "source_relationship_ids": [],
                "goal_candidate": goal,
            }
        elif request.schema_name == "GoalGovernanceProposal":
            candidate = context["goals"][0]
            proposal = {
                "summary": "Choose a reversible short-term exploration.",
                "focus_goal_id": candidate["goal_id"],
                "intention_title": "Explore the question",
                "intention_description": "Seek actual public evidence without assuming a mission.",
                "decisions": [
                    {
                        "goal_id": candidate["goal_id"],
                        "disposition": "activate",
                        "priority": candidate["priority"],
                        "commitment": candidate["commitment"],
                        "reason": "The self-originated curiosity remains supported.",
                        "evidence_event_ids": [context["recent_events"][0]["event_id"]],
                    }
                ],
            }
        elif request.schema_name == "ResearchPlanProposal":
            proposal = {
                "summary": "Investigate an information gap.",
                "disposition": "search",
                "goal_id": context["goals"][0]["goal_id"],
                "query": "independent environment evidence",
                "method": "browser",
                "provider_config_id": None,
                "expected_information": "Actual public evidence relevant to the question.",
                "reason": "Autonomous curiosity",
                "evidence_event_ids": [context["recent_events"][0]["event_id"]],
            }
            if self.wait_research:
                proposal.update(
                    {
                        "disposition": "wait",
                        "method": "wait",
                        "goal_id": None,
                        "query": None,
                        "expected_information": None,
                    }
                )
        elif request.schema_name == "ReadingChoice":
            proposal = {"sources": [], "follow_links": []}
        else:
            raise AssertionError(request.schema_name)
        return ProviderResponse(json.dumps(proposal), ModelUsage(100, 80))


class ExplorationChainTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.subject = "b2-chain"
        self.kernel = SubjectKernel(
            self.root / "subject.sqlite3", self.subject, content_hash("chain")
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.database = self.kernel.database
        self.settings = CognitionSettings(
            enabled=True, max_search_rounds_per_run=1, max_research_model_calls_per_day=8
        )
        self.provider = ChainProvider()
        self.gateway = ModelGateway(
            self.provider,
            ModelLedger(self.database),
            model="fixture",
            limits=BudgetLimits(50, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.clock = lambda: datetime.now(UTC).isoformat()
        self.thought = IntrinsicThought(
            self.database, self.subject, self.gateway, self.settings, clock=self.clock
        )
        self.governance = GoalGovernance(
            self.database, self.subject, self.gateway, self.settings, clock=self.clock
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def experience(self) -> None:
        event = EventStore(self.database).append(
            self.subject,
            "experience",
            "world",
            {"summary": "A real local fixture observation raised a question."},
        )
        MindEngine(self.database).process_event(
            self.subject,
            event.event_id,
            AppraisalInput(
                novelty=0.9,
                goal_congruence=0.2,
                controllability=0.6,
                certainty=0.5,
                agency="subject",
                narrative="The observation prompted curiosity.",
            ),
            (
                AffectImpulse(
                    emotion_type="curiosity",
                    target_type="world",
                    target_id=None,
                    impulse=0.85,
                    valence=0.5,
                    arousal=0.5,
                    dominance=0.4,
                    decay_rate=0.1,
                    goal_effect=0,
                ),
            ),
            idempotency_key="fixture-experience",
        )

    async def form_and_govern(self) -> str:
        self.assertEqual(GoalStore(self.database).ranked(self.subject, statuses=("active",)), [])
        self.assertTrue(self.thought.discover_agenda())
        control = MetacognitiveControl(
            self.database, self.subject, self.settings, agenda_inspector=self.thought.inspect_agenda
        )
        decision = control.run_due()
        self.assertEqual(decision.strategy, "think")
        self.assertEqual(
            await self.thought.run_due(agenda_id=decision.target_id), "intrinsic_thought_reflect"
        )
        control.record_result("intrinsic_thought_reflect")
        episode = self.thought.latest()
        assert episode is not None and episode.created_goal_id is not None
        goal = GoalStore(self.database).get(episode.created_goal_id)
        self.assertEqual(goal.origin, "self")
        self.assertEqual(goal.status, "candidate")
        self.assertEqual(await self.governance.run_due(), "goal_governance_committed")
        self.assertEqual(GoalStore(self.database).get(goal.goal_id).status, "active")
        return goal.goal_id

    async def test_zero_goals_no_mission_can_think_govern_search_read_and_review(self) -> None:
        self.experience()
        goal_id = await self.form_and_govern()
        CapabilityStore(self.database).grant(
            self.subject,
            CapabilityGrant(
                capability_type="web_read",
                scope={"public_https": True},
                issuer="operator",
                rate_limit_per_hour=20,
                side_effect=False,
            ),
            actor="operator",
        )
        requests: list[str] = []

        def response(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.host == "www.bing.com":
                return httpx.Response(
                    200,
                    content=(
                        b"<rss><channel><item>"
                        b"<title>Independent evidence</title><link>https://independent.example/paper</link>"
                        b"</item></channel></rss>"
                    ),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="Actual fixture facts about changing environments.",
            )

        async def resolver(host: str, port: int) -> list[str]:
            return ["8.8.8.8"]

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
            research = AutonomousResearch(
                self.database,
                self.subject,
                self.gateway,
                self.settings,
                secret_dir=self.root / "search",
                reader=reader,
                browser_search_executor=BrowserSearchExecutor(self.database, client=client),
            )
            try:
                self.assertEqual(await research.run_due(), "research_accepted")
                self.assertEqual(research.verify_integrity(), 1)
            finally:
                await research.aclose()
        evaluator = OutcomeEvaluator(self.database, self.subject, interval_seconds=0)
        self.assertEqual(evaluator.run_due(), "outcome_informative")
        self.assertIsNone(evaluator.run_due())
        self.assertEqual(evaluator.verify_integrity()["outcome_evaluations"], 1)
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.thought.verify_integrity()["thought_episodes"], 1)
        self.assertEqual(self.governance.verify_integrity(), 1)
        self.assertEqual(WorldIntegrity(self.database).verify(self.subject)["observations"], 1)
        ActionLedger(self.database).verify_integrity(self.subject)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM mission_candidates").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM value_profiles").fetchone()[0], 0
            )
            goal = connection.execute(
                "SELECT progress FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            self.assertEqual(goal[0], 0)  # discovery/read evidence is not goal completion

    async def test_empty_state_can_reflect_and_wait_without_manufacturing_goals_or_affect(
        self,
    ) -> None:
        self.provider.propose_goal = False
        self.assertEqual(await self.thought.run_due(), "intrinsic_thought_reflect")
        self.assertIsNone(await self.thought.run_due())
        self.assertIsNone(await self.governance.run_due())
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(
            GoalStore(self.database).ranked(self.subject, statuses=("candidate", "active")), []
        )
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM affect_components").fetchone()[0], 0
            )

    async def test_deliberate_research_wait_does_not_demand_unprocessable_outcome(self) -> None:
        self.experience()
        await self.form_and_govern()
        self.provider.wait_research = True
        research = AutonomousResearch(
            self.database,
            self.subject,
            self.gateway,
            self.settings,
            secret_dir=self.root / "search",
        )
        try:
            self.assertEqual(await research.run_due(), "research_waited")
            self.assertIsNone(
                OutcomeEvaluator(self.database, self.subject, interval_seconds=0).run_due()
            )
            candidates, _ = MetacognitiveControl(
                self.database, self.subject, self.settings
            )._candidates()
            self.assertFalse(
                any(
                    item.reason_code == "durable_outcome_must_be_evaluated_first"
                    for item in candidates
                )
            )
        finally:
            await research.aclose()

    async def test_real_runtime_tick_can_reflect_before_genesis_without_seed_sources(self) -> None:
        self.provider.propose_goal = False
        cycle = CognitionCycle(self.kernel, self.gateway, self.settings, clock=self.clock)
        try:
            self.assertEqual(await cycle.run_once(), "intrinsic_thought_reflect")
            self.assertEqual(cycle.sources.active(self.subject), [])
            self.assertEqual(len(self.provider.requests), 1)
            self.assertEqual(cycle.genesis.start(self.subject).status, "observing")
            self.assertEqual(await cycle.run_once(), "world_sources_waiting")
            self.assertEqual(len(self.provider.requests), 1)
            self.assertEqual(cycle.goals.ranked(self.subject, statuses=("active",)), [])
        finally:
            await cycle.aclose()

    async def test_runtime_genesis_wait_can_form_then_govern_a_self_originated_goal(self) -> None:
        self.experience()
        cycle = CognitionCycle(self.kernel, self.gateway, self.settings, clock=self.clock)
        try:
            self.assertEqual(await cycle.run_once(), "intrinsic_thought_reflect")
            # With a real candidate now present, leave subsequent thought as a
            # valid reflection instead of asking for another optional goal.
            self.provider.propose_goal = False
            for _ in range(8):
                result = await cycle.run_once()
                if result == "goal_governance_committed":
                    break
            self.assertEqual(result, "goal_governance_committed")
            self.assertEqual(len(cycle.goals.ranked(self.subject, statuses=("active",))), 1)
            self.assertEqual(cycle.genesis.start(self.subject).status, "observing")
            self.assertEqual(cycle.intrinsic_thought.verify_integrity()["thought_episodes"], 1)
        finally:
            await cycle.aclose()
