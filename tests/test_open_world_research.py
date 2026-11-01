from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from noyra.capability import CapabilityGrant, CapabilityStore, ToolRunner
from noyra.capability.errors import CapabilityDeniedError
from noyra.core import EventStore, SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.mind import GoalCandidate, GoalStore
from noyra.world import ObservationStore, SafeWebReader, SourceRegistry, WorldIntegrity
from noyra.world.errors import UnsafeSourceError, WorldStateConflictError


class OpenWorldTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.subject = "open-world-test"
        self.kernel = SubjectKernel(self.root / "noyra.sqlite3", self.subject, content_hash("b1"))
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.db = self.kernel.database
        self.sources = SourceRegistry(self.db)
        self.capabilities = CapabilityStore(self.db)
        self.tools = ToolRunner(self.db)
        self.source = self.sources.register(
            self.subject, "Candidate", "https://first.example/page", "web"
        )
        event = EventStore(self.db).append(self.subject, "interest", "self", {"topic": "evidence"})
        self.event = event
        goals = GoalStore(self.db)
        goal = goals.create_candidate(
            self.subject,
            GoalCandidate(
                title="Investigate evidence",
                description="Compare public sources",
                origin="self",
                priority=0.7,
                commitment=0.6,
                motive_emotion="curiosity",
            ),
            causal_source_ids=(event.event_id,),
            reason="curiosity",
        )
        self.goal = goals.activate(
            goal.goal_id, rationale="learn", causal_source_ids=(event.event_id,)
        )
        self.requests: list[str] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.reader = SafeWebReader(
            client=self.client, resolver=self.resolve, verify_peer_address=False
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.kernel.close()
        self.temp.cleanup()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<title>Evidence</title><p>Observed facts, not a search snippet.</p>"
                '<a href="https://second.example/paper#section">Independent paper</a>'
                '<a href="http://127.0.0.1/">unsafe</a>'
                '<a href="https://127.0.0.1/">unsafe</a>'
                '<a href="https://second.example/paper">duplicate</a>'
                '<script><a href="https://hidden.example/">hidden</a></script>'
            ),
        )

    @staticmethod
    async def resolve(host: str, port: int) -> list[str]:
        return ["8.8.8.8"]

    def grant(self) -> str:
        return self.capabilities.grant(
            self.subject,
            CapabilityGrant(
                capability_type="web_read",
                scope={"public_https": True},
                issuer="operator",
                rate_limit_per_hour=30,
                side_effect=False,
            ),
            actor="operator",
        ).grant_id

    async def test_candidate_trial_requires_capability_and_does_not_activate(self) -> None:
        with self.assertRaises(UnsafeSourceError):
            await self.reader.fetch(self.source)
        with self.assertRaises(CapabilityDeniedError):
            await self.tools.fetch_document(
                self.subject,
                self.source,
                self.reader,
                candidate_trial=True,
                goal_id=self.goal.goal_id,
                idempotency_key="denied",
            )
        self.assertEqual(self.requests, [])
        self.grant()
        result = await self.tools.fetch_document(
            self.subject,
            self.source,
            self.reader,
            candidate_trial=True,
            goal_id=self.goal.goal_id,
            idempotency_key="trial",
        )
        self.assertEqual(result.status, "succeeded")
        assert result.document is not None
        self.assertEqual(result.document.links, ("https://second.example/paper",))
        self.assertEqual(
            self.sources.get(self.source.source_id, subject_id=self.subject).status, "candidate"
        )
        self.assertEqual(len(self.requests), 1)
        replay = await self.tools.fetch_document(
            self.subject,
            self.source,
            self.reader,
            candidate_trial=True,
            goal_id=self.goal.goal_id,
            idempotency_key="trial",
        )
        self.assertIsNone(replay.document)
        self.assertEqual(len(self.requests), 1)
        with self.db.connection() as connection:
            action = ActionLedger._load_connection(connection, result.action_id)
        assert action.result is not None
        self.assertIn("observation_id", action.result)
        WorldIntegrity(self.db).verify(self.subject)

    async def test_trial_cannot_fetch_or_ingest_blocked_source(self) -> None:
        self.grant()
        blocked = self.sources.revise(
            self.source.source_id,
            subject_id=self.subject,
            trust_score=0.1,
            status="blocked",
            reason="operator block",
        )
        result = await self.tools.fetch_document(
            self.subject,
            blocked,
            self.reader,
            candidate_trial=True,
            goal_id=self.goal.goal_id,
            idempotency_key="blocked",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.requests, [])
        document = await self.reader.fetch_trial(self.source)
        with self.assertRaises(WorldStateConflictError):
            ObservationStore(self.db).ingest(
                self.subject, self.source.source_id, document, candidate_trial=True
            )

    async def test_revoke_during_trial_prevents_evidence_commit(self) -> None:
        grant_id = self.grant()

        async def revoke(request: httpx.Request) -> httpx.Response:
            self.capabilities.revoke(
                grant_id, reason="test", actor="operator", subject_id=self.subject
            )
            return await self.respond(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(revoke)) as client:
            reader = SafeWebReader(client=client, resolver=self.resolve, verify_peer_address=False)
            result = await self.tools.fetch_document(
                self.subject,
                self.source,
                reader,
                candidate_trial=True,
                goal_id=self.goal.goal_id,
                idempotency_key="revoke",
            )
        self.assertEqual(result.status, "failed")
        with self.db.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0
            )

    async def test_empty_and_over_budget_trials_produce_no_observation(self) -> None:
        self.grant()
        for text in ("<script>hidden</script>", "a" * 2000):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _, text=text: httpx.Response(
                        200, headers={"content-type": "text/html"}, text=text
                    )
                )
            ) as client:
                reader = SafeWebReader(
                    client=client, resolver=self.resolve, verify_peer_address=False
                )
                result = await self.tools.fetch_document(
                    self.subject,
                    self.source,
                    reader,
                    candidate_trial=True,
                    goal_id=self.goal.goal_id,
                    max_observation_chars=1024,
                    idempotency_key=content_hash(text),
                )
            self.assertEqual(result.status, "failed")
        with self.db.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0
            )

    async def test_trial_rejects_private_dns_and_preserves_default_ingestion_gate(self) -> None:
        self.grant()

        async def private(host: str, port: int) -> list[str]:
            return ["169.254.169.254"]

        reader = SafeWebReader(client=self.client, resolver=private, verify_peer_address=False)
        result = await self.tools.fetch_document(
            self.subject,
            self.source,
            reader,
            candidate_trial=True,
            goal_id=self.goal.goal_id,
            idempotency_key="private",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.requests, [])
        document = await self.reader.fetch_trial(self.source)
        with self.assertRaises(WorldStateConflictError):
            ObservationStore(self.db).ingest(self.subject, self.source.source_id, document)
