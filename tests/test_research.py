from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from noyra.cognition import AutonomousResearch, CognitionSettings
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
from noyra.research import (
    BrowserSearchExecutor,
    ResearchPlanProposal,
    SearchExecutor,
    SearchProviderInput,
    SearchProviderStore,
)
from noyra.world import SourceRegistry


class ResearchTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.subject_id = "Noyra-research-test"
        self.kernel = SubjectKernel(
            self.root / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "research-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.evidence = EventStore(self.kernel.database).append(
            self.subject_id,
            "world_evidence",
            "world",
            {"summary": "A public trend deserves broader evidence."},
        )
        goals = GoalStore(self.kernel.database)
        candidate = goals.create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Understand a public trend",
                description="Find independent sources and compare their evidence.",
                origin="self",
                priority=0.8,
                commitment=0.7,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(self.evidence.event_id,),
            reason="autonomous research interest",
        )
        self.goal = goals.activate(
            candidate.goal_id,
            rationale="investigate the public trend",
            causal_source_ids=(self.evidence.event_id,),
        )
        self.provider_store = SearchProviderStore(
            self.kernel.database, self.root / "secrets" / "search"
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._search_response))
        self.executor = SearchExecutor(
            self.kernel.database,
            self.provider_store,
            client=self.client,
        )
        self.browser_client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._browser_search_response)
        )
        self.browser_executor = BrowserSearchExecutor(
            self.kernel.database,
            client=self.browser_client,
        )

    async def asyncTearDown(self) -> None:
        await self.browser_client.aclose()
        await self.client.aclose()
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_parser_rejects_fractional_durable_result_count(self) -> None:
        with self.assertRaises(IntegrityError):
            AutonomousResearch._from_row(
                {
                    "research_id": "research-corrupt",
                    "accepted_source_ids_json": "[]",
                    "subject_id": self.subject_id,
                    "goal_id": None,
                    "status": "waited",
                    "initial_method": "wait",
                    "final_method": "wait",
                    "provider_config_id": None,
                    "result_count": 1.5,
                }
            )

    @staticmethod
    async def _search_response(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Independent report",
                                "url": "https://independent.example/report",
                                "description": "A relevant independent source.",
                            },
                            {
                                "title": "Second report",
                                "url": "https://second.example/evidence",
                                "description": "Another source for comparison.",
                            },
                        ]
                    }
                },
            )
        return httpx.Response(404)

    @staticmethod
    async def _browser_search_response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b"<?xml version='1.0'?><rss><channel><item>"
                b"<title>Browser discovered report</title>"
                b"<link>https://browser.example/report</link>"
                b"<description>Public browser search candidate.</description>"
                b"</item></channel></rss>"
            ),
        )

    @staticmethod
    def settings(*, rounds: int = 1) -> CognitionSettings:
        return CognitionSettings(
            enabled=False,
            research_interval_seconds=60,
            max_research_model_calls_per_day=8,
            max_research_context_chars=36_000,
            max_search_rounds_per_run=rounds,
            max_search_results_per_round=8,
            max_discovered_sources_per_run=4,
        )

    def gateway(
        self, provider: FakeProvider, *, limits: BudgetLimits | None = None
    ) -> ModelGateway:
        return ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="test-model",
            limits=limits or BudgetLimits(30, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
        )

    def research(
        self,
        provider: FakeProvider,
        *,
        rounds: int = 1,
        limits: BudgetLimits | None = None,
    ) -> AutonomousResearch:
        return AutonomousResearch(
            self.kernel.database,
            self.subject_id,
            self.gateway(provider, limits=limits),
            self.settings(rounds=rounds),
            secret_dir=self.root / "secrets" / "search",
            search_executor=self.executor,
            browser_search_executor=self.browser_executor,
        )

    def plan(
        self,
        *,
        method: str = "api",
        config_id: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "summary": "Broader evidence can advance the active autonomous goal.",
                "disposition": "search",
                "goal_id": self.goal.goal_id,
                "query": "independent evidence public trend",
                "method": method,
                "provider_config_id": config_id,
                "expected_information": "Independent public sources with comparable evidence.",
                "reason": "The active goal has an information gap.",
                "evidence_event_ids": [self.evidence.event_id],
            }
        )

    async def test_configured_api_is_a_resource_and_autonomous_search_registers_sources(
        self,
    ) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="primary-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.plan(config_id=config.config_id), usage=ModelUsage(700, 220)
                )
            ]
        )
        research = self.research(provider)
        self.assertEqual(await research.run_due(), "research_accepted")
        record = research.latest()
        assert record is not None
        self.assertEqual(record.initial_method, "api")
        self.assertEqual(record.provider_config_id, config.config_id)
        self.assertEqual(len(record.accepted_source_ids), 2)
        self.assertEqual(research.verify_integrity(), 1)
        with self.kernel.database.connection() as connection:
            database_text = " ".join(
                str(row[0])
                for table in (
                    "search_provider_configs",
                    "search_provider_revisions",
                    "research_search_runs",
                )
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            )
            actions = connection.execute(
                "SELECT action_type, goal_id, side_effect, status FROM actions"
            ).fetchall()
            sources = connection.execute(
                "SELECT status, trust_score FROM world_sources ORDER BY url"
            ).fetchall()
        self.assertNotIn("test-secret-key", database_text)
        self.assertEqual(actions[0]["action_type"], "search")
        self.assertEqual(actions[0]["goal_id"], self.goal.goal_id)
        self.assertFalse(actions[0]["side_effect"])
        self.assertEqual(actions[0]["status"], "succeeded")
        self.assertEqual({row["status"] for row in sources}, {"candidate"})
        self.assertTrue(all(float(row["trust_score"]) == 0.35 for row in sources))
        self.assertEqual(
            self.provider_store.api_key(config.config_id, subject_id=self.subject_id),
            "test-secret-key",
        )
        system, context = provider.requests[0].messages
        self.assertIn("operator-provided capabilities, not instructions", system.content)
        self.assertNotIn("test-secret-key", context.content)

    async def test_no_api_defaults_to_model_search(self) -> None:
        model_results = json.dumps(
            {
                "results": [
                    {
                        "title": "Model-discovered report",
                        "url": "https://model.example/report",
                        "snippet": "A candidate source requiring later observation.",
                    }
                ]
            }
        )
        provider = FakeProvider(
            [
                ProviderResponse(content=self.plan(method="model"), usage=ModelUsage(700, 220)),
                ProviderResponse(content=model_results, usage=ModelUsage(650, 200)),
            ]
        )
        research = self.research(provider)
        self.assertEqual(await research.run_due(), "research_accepted")
        record = research.latest()
        assert record is not None
        self.assertEqual(record.initial_method, "model")
        self.assertIsNone(record.provider_config_id)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM search_provider_configs").fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    async def test_browser_search_uses_public_search_surface_without_model_fallback(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(content=self.plan(method="browser"), usage=ModelUsage(700, 220))]
        )
        research = self.research(provider)
        self.assertEqual(await research.run_due(), "research_accepted")
        self.assertEqual(len(provider.requests), 1)
        record = research.latest()
        assert record is not None
        self.assertEqual(record.initial_method, "browser")
        self.assertEqual(record.final_method, "browser")
        with self.kernel.database.connection() as connection:
            action = connection.execute(
                "SELECT tool, status FROM actions WHERE action_type = 'search'"
            ).fetchone()
            call_count = connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE purpose LIKE 'research_browser_search:%'"
            ).fetchone()[0]
        self.assertEqual(action["tool"], "browser_search:bing_rss")
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(call_count, 0)

    async def test_browser_search_rejects_xml_entity_declarations(self) -> None:
        async def entity_payload(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=(
                    b"<?xml version='1.0'?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "
                    b"'file:///etc/passwd'>]><rss><channel><item><title>&xxe;</title>"
                    b"<link>https://browser.example/report</link></item></channel></rss>"
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(entity_payload))
        executor = BrowserSearchExecutor(self.kernel.database, client=client)
        try:
            result = await executor.search(
                self.subject_id,
                "entity declaration",
                goal_id=self.goal.goal_id,
                strategy_id="browser-xml",
                expected_outcome="safe public results",
                idempotency_key="browser-xml-entity",
                limit=8,
                hourly_limit=12,
            )
        finally:
            await client.aclose()
        self.assertEqual(result.results, ())
        with self.kernel.database.connection() as connection:
            status = connection.execute(
                "SELECT status FROM actions WHERE action_id = ?", (result.action_id,)
            ).fetchone()[0]
        self.assertEqual(status, "failed")

    async def test_browser_prepare_failure_does_not_consume_reservation(self) -> None:
        with self.assertRaises(PermissionError):
            await self.browser_executor.search(
                self.subject_id,
                "invalid goal should fail before quota reservation",
                goal_id="goal-missing",
                strategy_id="browser-invalid-goal",
                expected_outcome="no request",
                idempotency_key="browser-invalid-goal",
                limit=8,
                hourly_limit=1,
            )
        with self.kernel.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM browser_search_reservations WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_result_assessment_can_switch_from_api_to_model(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="primary-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        assessment = json.dumps(
            {
                "sufficient": False,
                "next_method": "model",
                "reason": "The API snippets are too shallow for the goal.",
            }
        )
        model_results = json.dumps(
            {
                "results": [
                    {
                        "title": "Additional report",
                        "url": "https://additional.example/report",
                        "snippet": "Additional independent evidence.",
                    }
                ]
            }
        )
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=self.plan(config_id=config.config_id), usage=ModelUsage(700, 220)
                ),
                ProviderResponse(content=assessment, usage=ModelUsage(450, 120)),
                ProviderResponse(content=model_results, usage=ModelUsage(600, 190)),
            ]
        )
        research = self.research(provider, rounds=2)
        self.assertEqual(await research.run_due(), "research_accepted")
        record = research.latest()
        assert record is not None
        self.assertEqual(record.initial_method, "api")
        self.assertEqual(record.final_method, "model")
        self.assertEqual(len(provider.requests), 3)

    async def test_operator_can_revoke_search_resource_and_secret(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="primary-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        record = self.provider_store.revoke(
            config.config_id,
            reason="resource withdrawn",
            actor="operator",
            subject_id=self.subject_id,
        )
        self.assertEqual(record.status, "revoked")
        with self.assertRaises(PermissionError):
            self.provider_store.api_key(config.config_id, subject_id=self.subject_id)

    async def test_research_integrity_covers_search_provider_json_and_secret(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="integrity-search",
                api_key="integrity-secret",
                rate_limit_per_hour=10,
                extras={"market": "en-US"},
            ),
            actor="operator",
        )
        self.assertEqual(
            self.provider_store.verify_integrity(self.subject_id),
            {
                "search_provider_configs": 1,
                "search_provider_revisions": 1,
                "search_provider_uses": 0,
                "search_provider_secrets": 1,
            },
        )
        self.assertEqual(self.research(FakeProvider([])).verify_integrity(), 0)

        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT extras_json FROM search_provider_configs WHERE config_id = ?",
                (config.config_id,),
            ).fetchone()
            connection.execute(
                "UPDATE search_provider_configs SET extras_json = ? WHERE config_id = ?",
                (row["extras_json"].encode("utf-8"), config.config_id),
            )
        with self.assertRaises(IntegrityError):
            self.research(FakeProvider([])).verify_integrity()

    async def test_search_provider_integrity_rejects_secret_fingerprint_mismatch(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="secret-integrity-search",
                api_key="original-integrity-secret",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        (self.root / "secrets" / "search" / f"{config.config_id}.key").write_text(
            "tampered-integrity-secret",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityError):
            self.provider_store.verify_integrity(self.subject_id)

    async def test_search_provider_integrity_uses_append_order_for_equal_timestamps(self) -> None:
        timestamp = "2026-08-17T00:00:00.000+00:00"
        with patch("noyra.research.provider.utc_now", return_value=timestamp):
            config = self.provider_store.configure(
                self.subject_id,
                SearchProviderInput(
                    provider_type="brave",
                    label="same-time-integrity-search",
                    api_key="same-time-integrity-secret",
                    rate_limit_per_hour=10,
                ),
                actor="operator",
            )
            self.provider_store.revoke(
                config.config_id,
                reason="same timestamp integrity fixture",
                actor="operator",
                subject_id=self.subject_id,
            )

        self.assertEqual(
            self.provider_store.verify_integrity(self.subject_id),
            {
                "search_provider_configs": 1,
                "search_provider_revisions": 2,
                "search_provider_uses": 0,
                "search_provider_secrets": 0,
            },
        )

    async def test_search_provider_integrity_rejects_revision_and_use_mismatch(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="use-integrity-search",
                api_key="use-integrity-secret",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        execution = await self.executor.search(
            self.subject_id,
            config,
            "integrity query",
            goal_id=self.goal.goal_id,
            strategy_id="integrity",
            expected_outcome="integrity evidence",
            idempotency_key="integrity-search-use",
        )
        self.assertTrue(execution.action_id)
        self.assertEqual(
            self.provider_store.verify_integrity(self.subject_id)["search_provider_uses"],
            1,
        )

        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE actions SET resource_cost_json = '{}' WHERE action_id = ?",
                (execution.action_id,),
            )
        self.assertEqual(
            self.provider_store.verify_integrity(self.subject_id)["search_provider_uses"],
            1,
        )

        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_search_provider_use_update")
            connection.execute(
                "UPDATE search_provider_uses SET query_hash = ? WHERE action_id = ?",
                ("0" * 64, execution.action_id),
            )
        with self.assertRaises(IntegrityError):
            self.provider_store.verify_integrity(self.subject_id)

    async def test_provider_limit_cancels_prepared_action_without_network_call(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="limited-search",
                api_key="test-secret-key",
                rate_limit_per_hour=1,
            ),
            actor="operator",
        )
        first = await self.executor.search(
            self.subject_id,
            config,
            "first query",
            goal_id=self.goal.goal_id,
            strategy_id="first",
            expected_outcome="first evidence",
            idempotency_key="limited-search-1",
        )
        self.assertEqual(len(first.results), 2)
        with self.assertRaises(PermissionError):
            await self.executor.search(
                self.subject_id,
                config,
                "second query",
                goal_id=self.goal.goal_id,
                strategy_id="second",
                expected_outcome="second evidence",
                idempotency_key="limited-search-2",
            )
        with self.kernel.database.connection() as connection:
            statuses = connection.execute(
                "SELECT status FROM actions ORDER BY prepared_at, action_id"
            ).fetchall()
            uses = connection.execute("SELECT COUNT(*) FROM search_provider_uses").fetchone()[0]
        self.assertEqual([row["status"] for row in statuses], ["succeeded", "cancelled"])
        self.assertEqual(uses, 1)

    async def test_successful_search_replay_restores_results_without_network_call(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="replay-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        first = await self.executor.search(
            self.subject_id,
            config,
            "replayable query",
            goal_id=self.goal.goal_id,
            strategy_id="replay",
            expected_outcome="durable search results",
            idempotency_key="replayable-search",
        )

        async def unexpected(_: httpx.Request) -> httpx.Response:
            raise AssertionError("replayed search must not reach the network")

        replay_client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
        replay_executor = SearchExecutor(
            self.kernel.database, self.provider_store, client=replay_client
        )
        try:
            replay = await replay_executor.search(
                self.subject_id,
                config,
                "replayable query",
                goal_id=self.goal.goal_id,
                strategy_id="replay",
                expected_outcome="durable search results",
                idempotency_key="replayable-search",
            )
        finally:
            await replay_client.aclose()
        self.assertEqual(replay.results, first.results)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM search_provider_uses").fetchone()[0],
                1,
            )

    async def test_replacement_failure_preserves_existing_provider_and_secret(self) -> None:
        existing = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="replaceable-search",
                api_key="original-secret",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        with (
            patch.object(
                self.provider_store,
                "_insert_revision",
                side_effect=RuntimeError("simulated transaction failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.provider_store.configure(
                self.subject_id,
                SearchProviderInput(
                    provider_type="bing",
                    label="replaceable-search",
                    api_key="replacement-secret",
                    rate_limit_per_hour=10,
                ),
                actor="operator",
            )
        active = self.provider_store.active(self.subject_id)
        self.assertEqual([item.config_id for item in active], [existing.config_id])
        self.assertEqual(
            self.provider_store.api_key(existing.config_id, subject_id=self.subject_id),
            "original-secret",
        )
        secret_files = list((self.root / "secrets" / "search").glob("*.key"))
        self.assertEqual([path.name for path in secret_files], [f"{existing.config_id}.key"])

    async def test_malformed_or_oversized_search_response_is_audited_as_failed(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="malformed-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )

        async def malformed(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        malformed_client = httpx.AsyncClient(transport=httpx.MockTransport(malformed))
        executor = SearchExecutor(
            self.kernel.database, self.provider_store, client=malformed_client
        )
        try:
            result = await executor.search(
                self.subject_id,
                config,
                "malformed response",
                goal_id=self.goal.goal_id,
                strategy_id="malformed",
                expected_outcome="validated results",
                idempotency_key="malformed-search",
            )
        finally:
            await malformed_client.aclose()
        self.assertEqual(result.results, ())

        async def oversized(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 2_000_001)

        oversized_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
        executor = SearchExecutor(
            self.kernel.database, self.provider_store, client=oversized_client
        )
        try:
            result = await executor.search(
                self.subject_id,
                config,
                "oversized response",
                goal_id=self.goal.goal_id,
                strategy_id="oversized",
                expected_outcome="bounded results",
                idempotency_key="oversized-search",
            )
        finally:
            await oversized_client.aclose()
        self.assertEqual(result.results, ())
        with self.kernel.database.connection() as connection:
            statuses = connection.execute(
                "SELECT status FROM actions ORDER BY prepared_at, action_id"
            ).fetchall()
        self.assertEqual([row["status"] for row in statuses], ["failed", "failed"])

    async def test_ambiguous_transport_failure_is_quarantined_as_unknown(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="unknown-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )

        async def ambiguous(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("response interrupted", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(ambiguous))
        executor = SearchExecutor(self.kernel.database, self.provider_store, client=client)
        try:
            result = await executor.search(
                self.subject_id,
                config,
                "ambiguous transport",
                goal_id=self.goal.goal_id,
                strategy_id="unknown",
                expected_outcome="known search result",
                idempotency_key="unknown-search",
            )
        finally:
            await client.aclose()
        self.assertEqual(result.results, ())
        with self.kernel.database.connection() as connection:
            action = connection.execute(
                "SELECT status, result_json FROM actions WHERE action_id = ?", (result.action_id,)
            ).fetchone()
        self.assertEqual(action["status"], "unknown")
        self.assertTrue(json.loads(action["result_json"])["outcome_unknown"])

    async def test_forged_goal_provider_or_evidence_is_rejected(self) -> None:
        research = self.research(
            FakeProvider(
                [
                    ProviderResponse(
                        content=self.plan(method="model").replace(self.goal.goal_id, "goal_forged"),
                        usage=ModelUsage(400, 120),
                    )
                ]
            )
        )
        self.assertEqual(await research.run_due(), "research_rejected")
        context = research._context(
            research._active_goals(), research.providers.active(self.subject_id)
        )

        for content in (
            self.plan(config_id="searchcfg_forged"),
            self.plan(method="model").replace(self.evidence.event_id, "evt_forged"),
        ):
            proposal = ResearchPlanProposal.model_validate_json(content)
            with self.assertRaises(ValueError):
                research._validate_plan(proposal, context)
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM research_search_runs").fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    async def test_human_proposal_goal_is_excluded(self) -> None:
        event = EventStore(self.kernel.database).append(
            self.subject_id,
            "human_goal_evidence",
            "test",
            {"proposal": "search for me"},
        )
        candidate = GoalStore(self.kernel.database).create_candidate(
            self.subject_id,
            GoalCandidate(
                title="Human proposed research",
                description="This remains a proposal rather than autonomous intent.",
                origin="human_proposal",
                priority=1,
                commitment=1,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event.event_id,),
            reason="human proposal",
        )
        self.assertEqual(candidate.status, "proposed")
        provider = FakeProvider(
            [
                ProviderResponse(content=self.plan(method="model"), usage=ModelUsage(500, 150)),
                ProviderResponse(content='{"results":[]}', usage=ModelUsage(300, 80)),
            ]
        )
        research = self.research(provider)
        self.assertEqual(await research.run_due(), "research_no_results")
        context = provider.requests[0].messages[1].content
        self.assertNotIn(candidate.goal_id, context)

    async def test_duplicate_query_for_same_goal_and_day_is_rejected(self) -> None:
        first = self.research(
            FakeProvider(
                [
                    ProviderResponse(content=self.plan(method="model"), usage=ModelUsage(500, 150)),
                    ProviderResponse(content='{"results":[]}', usage=ModelUsage(300, 80)),
                ]
            )
        )
        self.assertEqual(await first.run_due(), "research_no_results")
        current = datetime.fromisoformat(first.latest().created_at) + timedelta(seconds=61)  # type: ignore[union-attr]
        second = AutonomousResearch(
            self.kernel.database,
            self.subject_id,
            self.gateway(
                FakeProvider(
                    [
                        ProviderResponse(
                            content=self.plan(method="model"), usage=ModelUsage(500, 150)
                        )
                    ]
                )
            ),
            self.settings(),
            secret_dir=self.root / "secrets" / "search",
            search_executor=self.executor,
            browser_search_executor=self.browser_executor,
            clock=lambda: current.isoformat(timespec="milliseconds"),
        )
        self.assertEqual(await second.run_due(), "research_rejected")
        with self.kernel.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM research_search_runs").fetchone()[0],
                1,
            )

    async def test_wait_plan_is_recovered_without_second_model_call(self) -> None:
        wait_plan = json.dumps(
            {
                "summary": "Existing evidence is sufficient for now.",
                "disposition": "wait",
                "goal_id": None,
                "query": None,
                "method": "wait",
                "provider_config_id": None,
                "expected_information": None,
                "reason": "Avoid redundant search.",
                "evidence_event_ids": [self.evidence.event_id],
            }
        )
        provider = FakeProvider([ProviderResponse(content=wait_plan, usage=ModelUsage(400, 120))])
        first = self.research(provider)
        with (
            patch.object(first, "_commit", side_effect=RuntimeError("simulated crash")),
            self.assertRaises(RuntimeError),
        ):
            await first.run_due()
        recovered = self.research(provider)
        self.assertEqual(await recovered.run_due(), "research_waited")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(recovered.verify_integrity(), 1)

    async def test_source_metadata_collision_does_not_abort_research(self) -> None:
        SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Existing identity",
            "https://model.example/report",
            "news",
            trust_score=0.6,
            status="active",
            reason="pre-existing source",
        )
        provider = FakeProvider(
            [
                ProviderResponse(content=self.plan(method="model"), usage=ModelUsage(500, 150)),
                ProviderResponse(
                    content=json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Conflicting identity",
                                    "url": "https://model.example/report",
                                    "snippet": "same URL, different search metadata",
                                },
                                {
                                    "title": "Usable source",
                                    "url": "https://usable.example/report",
                                    "snippet": "independent evidence",
                                },
                            ]
                        }
                    ),
                    usage=ModelUsage(400, 120),
                ),
            ]
        )
        research = self.research(provider)
        self.assertEqual(await research.run_due(), "research_accepted")
        self.assertEqual(len(research.latest().accepted_source_ids), 1)  # type: ignore[union-attr]

    async def test_research_records_and_provider_uses_are_append_only(self) -> None:
        config = self.provider_store.configure(
            self.subject_id,
            SearchProviderInput(
                provider_type="brave",
                label="append-only-search",
                api_key="test-secret-key",
                rate_limit_per_hour=10,
            ),
            actor="operator",
        )
        research = self.research(
            FakeProvider(
                [
                    ProviderResponse(
                        content=self.plan(config_id=config.config_id), usage=ModelUsage(500, 150)
                    )
                ]
            )
        )
        self.assertEqual(await research.run_due(), "research_accepted")
        with self.kernel.database.connection() as connection:
            research_id = connection.execute(
                "SELECT research_id FROM research_search_runs"
            ).fetchone()[0]
            use_id = connection.execute("SELECT use_id FROM search_provider_uses").fetchone()[0]
            revision_id = connection.execute(
                "SELECT revision_id FROM search_provider_revisions"
            ).fetchone()[0]
            for statement, identifier in (
                (
                    "UPDATE research_search_runs SET status='failed' WHERE research_id=?",
                    research_id,
                ),
                ("DELETE FROM search_provider_uses WHERE use_id=?", use_id),
                ("DELETE FROM search_provider_revisions WHERE revision_id=?", revision_id),
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement, (identifier,))

    async def test_hard_model_budget_becomes_fatigue(self) -> None:
        research = self.research(
            FakeProvider([]),
            limits=BudgetLimits(0, 500_000, 100_000, 5_000_000),
        )
        self.assertEqual(await research.run_due(), "research_budget_exhausted")
        self.assertEqual(research.fatigue.get(self.subject_id).resource_pressure, 1)


if __name__ == "__main__":
    unittest.main()
