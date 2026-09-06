from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
from noyra.core import ActionLedger, EventStore, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, utc_now
from noyra.interaction import PublicProjection
from noyra.learning import OutcomeEvaluator
from noyra.mind import GoalCandidate, GoalStore
from noyra.research import BrowserSearchExecutor
from noyra.world import ObservationStore, SafeWebReader, SourceRegistry


class OutcomeLearningTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-outcome-learning-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "outcome-learning-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = utc_now()
        self.evidence = EventStore(self.kernel.database).append(
            self.subject_id,
            "learning_evidence",
            "test",
            {"reason": "evaluate a real result"},
        )
        candidate = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Learn from verified public evidence",
                description="Use real observations to refine a bounded strategy.",
                origin="self",
                priority=0.8,
                commitment=0.7,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.evidence.event_id,),
            reason="test outcome learning",
        )
        self.goal = GoalStore(self.kernel.database).activate(
            candidate.goal_id,
            rationale="test active goal",
            causal_source_ids=(self.evidence.event_id,),
        )
        self.source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Verified report",
            "https://example.com/report",
            "news",
            trust_score=0.7,
            status="active",
            reason="test source",
        )

    async def asyncTearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_profile_parser_rejects_non_finite_durable_confidence(self) -> None:
        with self.assertRaises(IntegrityError):
            OutcomeEvaluator._profile_from_row(
                {
                    "profile_id": "profile-corrupt",
                    "attempts": 1,
                    "successes": 1,
                    "failures": 0,
                    "inconclusive": 0,
                    "confidence": float("nan"),
                }
            )

    def evaluator(self, *, progress_delta: float = 0.05) -> OutcomeEvaluator:
        return OutcomeEvaluator(
            self.kernel.database,
            self.subject_id,
            clock=lambda: self.clock_value,
            interval_seconds=0,
            max_progress_delta=progress_delta,
        )

    async def test_new_observation_advances_goal_and_strategy_confidence(self) -> None:
        CapabilityStore(self.kernel.database).grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="web_read",
                scope={"hosts": ["example.com"]},
                issuer="operator",
                rate_limit_per_hour=10,
                side_effect=False,
                requires_approval=False,
            ),
            actor="operator",
        )

        async def response(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="New independently verified evidence.",
            )

        async def resolver(host: str, port: int) -> tuple[str, ...]:
            del host, port
            return ("93.184.216.34",)

        client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
        try:
            action = await ToolRunner(self.kernel.database).fetch_document(
                self.subject_id,
                self.source,
                reader,
                idempotency_key="learning-action",
                goal_id=self.goal.goal_id,
                strategy_id="strategy-learning",
                expected_outcome="new evidence",
                public_goal_reference=self.goal.goal_id,
            )
        finally:
            await client.aclose()
        assert action.document is not None
        observation, _ = ObservationStore(self.kernel.database).record(
            self.subject_id, self.source.source_id, action.document
        )
        self._insert_deliberation(
            action.action_id,
            observation.observation_id,
            status="succeeded",
        )

        evaluator = self.evaluator()
        self.assertIsNone(evaluator.run_due())
        ObservationStore(self.kernel.database).mark(
            observation.observation_id,
            "analyzed",
            reason="test cognition accepted the observation",
            subject_id=self.subject_id,
        )
        self.assertEqual(evaluator.run_due(), "outcome_progress")
        goal = GoalStore(self.kernel.database).get(self.goal.goal_id)
        profile = evaluator.profiles(self.goal.goal_id)[0]
        evaluation = evaluator.latest()
        assert evaluation is not None
        self.assertEqual(goal.progress, 0.05)
        self.assertGreater(profile.confidence, 0.5)
        self.assertEqual(profile.last_outcome, "progress")
        self.assertEqual(evaluation.observation_id, observation.observation_id)
        integrity = evaluator.verify_integrity()
        self.assertEqual(integrity["outcome_evaluations"], 1)
        self.assertEqual(integrity["strategy_profile_revisions"], 1)
        projection = PublicProjection(self.kernel.database)
        public = projection.outcomes_view(self.subject_id)[0]
        self.assertEqual(public["outcome"], "progress")
        self.assertNotIn("rationale_code", public)
        self.assertEqual(
            projection.private_state(self.subject_id)["learning_summary"]["evaluation_count"], 1
        )
        self.assertIsNone(evaluator.run_due())

    async def test_candidate_search_is_informative_but_cannot_claim_goal_progress(self) -> None:
        async def browser_response(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=(
                    b"<?xml version='1.0'?><rss><channel><item>"
                    b"<title>Candidate report</title>"
                    b"<link>https://candidate.example/report</link>"
                    b"</item></channel></rss>"
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(browser_response))
        executor = BrowserSearchExecutor(self.kernel.database, client=client)
        try:
            search = await executor.search(
                self.subject_id,
                "candidate evidence",
                goal_id=self.goal.goal_id,
                strategy_id="browser-strategy",
                expected_outcome="candidate sources",
                idempotency_key="learning-browser-search",
                limit=8,
                hourly_limit=12,
            )
        finally:
            await client.aclose()
        now = self.clock_value
        accepted = ["src_candidate"]
        plan = {
            "summary": "candidate discovery",
            "disposition": "search",
            "goal_id": self.goal.goal_id,
            "query": "candidate evidence",
            "method": "browser",
            "provider_config_id": None,
            "expected_information": "candidate sources",
            "reason": "information gap",
            "evidence_event_ids": [self.evidence.event_id],
        }
        with self.kernel.database.transaction() as connection:
            model_call_id = self._insert_model_call(connection, "research-planner")
            connection.execute(
                """INSERT INTO research_search_runs(
                    research_id, subject_id, planner_call_id, goal_id, idempotency_key,
                    status, initial_method, final_method, provider_config_id, query_hash,
                    result_count, accepted_source_ids_json, rounds_json, plan_json,
                    plan_hash, state_hash, created_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'accepted', 'browser', 'browser', NULL,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    "research_learning",
                    self.subject_id,
                    model_call_id,
                    self.goal.goal_id,
                    "research-learning",
                    content_hash("candidate evidence"),
                    len(search.results),
                    json.dumps(accepted),
                    json.dumps(
                        [
                            {
                                "round": 1,
                                "method": "browser",
                                "provider_config_id": None,
                                "action_id": search.action_id,
                                "result_count": len(search.results),
                                "result_hash": content_hash(
                                    [item.__dict__ for item in search.results]
                                ),
                            }
                        ]
                    ),
                    json.dumps(plan),
                    content_hash(plan),
                    "fixture-state-hash",
                    now,
                    now,
                ),
            )
        evaluator = self.evaluator()
        self.assertEqual(evaluator.run_due(), "outcome_informative")
        self.assertEqual(GoalStore(self.kernel.database).get(self.goal.goal_id).progress, 0)
        profile = evaluator.profiles(self.goal.goal_id)[0]
        self.assertEqual(profile.last_outcome, "informative")
        self.assertGreater(profile.confidence, 0.5)

    def test_failed_action_lowers_strategy_confidence_without_progress(self) -> None:
        actions = ActionLedger(self.kernel.database)
        action = actions.prepare(
            self.subject_id,
            "web_read",
            "web.read",
            "https://example.com/failure",
            {"source": "failure"},
            goal_id=self.goal.goal_id,
            strategy_id="strategy-failure",
            expected_outcome="evidence",
            idempotency_key="failed-learning-action",
        )
        actions.start(action.action_id)
        actions.finish(action.action_id, "failed", {"error": "test"})
        self._insert_deliberation(action.action_id, None, status="failed")
        evaluator = self.evaluator()
        self.assertEqual(evaluator.run_due(), "outcome_failure")
        profile = evaluator.profiles(self.goal.goal_id)[0]
        self.assertLess(profile.confidence, 0.5)
        self.assertEqual(GoalStore(self.kernel.database).get(self.goal.goal_id).progress, 0)

    def test_records_are_append_only_and_integrity_checked(self) -> None:
        actions = ActionLedger(self.kernel.database)
        action = actions.prepare(
            self.subject_id,
            "web_read",
            "web.read",
            "https://example.com/unchanged",
            {"source": "unchanged"},
            goal_id=self.goal.goal_id,
            strategy_id="strategy-unchanged",
            expected_outcome="new evidence",
            idempotency_key="unchanged-learning-action",
        )
        actions.start(action.action_id)
        actions.finish(action.action_id, "succeeded", {"unchanged": True})
        self._insert_deliberation(action.action_id, None, status="unchanged")
        evaluator = self.evaluator()
        self.assertEqual(evaluator.run_due(), "outcome_no_change")
        latest = evaluator.latest()
        assert latest is not None
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE outcome_evaluations SET outcome = 'failure' WHERE evaluation_id = ?",
                (latest.evaluation_id,),
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM strategy_profile_revisions")

    def _insert_deliberation(
        self, action_id: str, observation_id: str | None, *, status: str
    ) -> None:
        proposal = {
            "summary": "evaluate fixture action",
            "disposition": "investigate",
            "goal_id": self.goal.goal_id,
            "source_id": "src_fixture",
            "strategy_title": "fixture strategy",
            "expected_observation": "verified evidence",
            "reason": "fixture",
            "evidence_event_ids": [self.evidence.event_id],
        }
        now = self.clock_value
        with self.kernel.database.transaction() as connection:
            call_id = self._insert_model_call(connection, f"action-{action_id}")
            connection.execute(
                """INSERT INTO action_deliberation_runs(
                    deliberation_id, subject_id, model_call_id, goal_id, source_id,
                    action_id, observation_id, idempotency_key, status, strategy_title,
                    expected_observation, summary, reason, evidence_event_ids_json,
                    proposal_json, proposal_hash, result_hash, state_hash, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"adel_{action_id}",
                    self.subject_id,
                    call_id,
                    self.goal.goal_id,
                    self.source.source_id,
                    action_id,
                    observation_id,
                    f"adel-{action_id}",
                    status,
                    "fixture strategy",
                    "verified evidence",
                    "evaluate fixture action",
                    "fixture",
                    json.dumps([self.evidence.event_id]),
                    json.dumps(proposal),
                    content_hash(proposal),
                    content_hash(
                        {"action_id": action_id, "observation_id": observation_id, "status": status}
                    ),
                    "fixture-state-hash",
                    now,
                    now,
                ),
            )

    def _insert_model_call(self, connection: sqlite3.Connection, suffix: str) -> str:
        call_id = f"call_{content_hash(suffix)[:16]}"
        response = {
            "content": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cost_microusd": 0,
            "finish_reason": "stop",
            "provider_request_id": None,
            "attempts": 1,
        }
        connection.execute(
            """INSERT INTO model_calls(
                call_id, subject_id, provider, model, purpose, request_hash, idempotency_key,
                status, response_json, response_hash, usage_estimated, error_code,
                created_at, completed_at
            ) VALUES (?, ?, 'fake', 'test', ?, ?, ?, 'succeeded', ?, ?, 0, NULL, ?, ?)""",
            (
                call_id,
                self.subject_id,
                suffix,
                content_hash(suffix),
                f"fixture-{suffix}",
                json.dumps(response),
                content_hash(response),
                self.clock_value,
                self.clock_value,
            ),
        )
        return call_id


if __name__ == "__main__":
    unittest.main()
