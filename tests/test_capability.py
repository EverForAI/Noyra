from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from noyra.autonomy import AutonomyLoop, DurableWorkflowStore, LoopConfig
from noyra.capability import (
    CapabilityGrant,
    CapabilityIntegrity,
    CapabilityStore,
    CapabilityType,
    ToolRunner,
)
from noyra.capability.errors import CapabilityDeniedError
from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash
from noyra.sleep import FatigueInputs, FatigueTracker
from noyra.world import SafeWebReader, SourceRegistry


class CapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "authorized"
        self.root.mkdir()
        self.db_path = Path(self.temp_dir.name) / "noyra.sqlite3"
        self.subject_id = "Noyra-capability-test"
        self.genesis_hash = content_hash({"seed": "capability-test"})
        self.kernel = SubjectKernel(self.db_path, self.subject_id, self.genesis_hash)
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.capabilities = CapabilityStore(self.kernel.database)
        self.tools = ToolRunner(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_authorized_atomic_write_and_read_with_rate_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.capabilities.grant(
                self.subject_id,
                CapabilityGrant(
                    capability_type="filesystem_write",
                    scope={"root": str(self.root)},
                    issuer="workspace-owner",
                    rate_limit_per_hour=2,
                    side_effect=True,
                    requires_approval=True,
                ),
                actor="operator",
            )
        self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="filesystem_write",
                scope={"root": str(self.root)},
                issuer="workspace-owner",
                rate_limit_per_hour=2,
                side_effect=True,
            ),
            actor="operator",
        )
        self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="filesystem_read",
                scope={"root": str(self.root)},
                issuer="workspace-owner",
                rate_limit_per_hour=1,
                side_effect=False,
            ),
            actor="operator",
        )
        target = self.root / "note.txt"
        written = self.tools.write_text(
            self.subject_id,
            target,
            "private note",
            idempotency_key="write-1",
        )
        self.assertEqual(written.status, "succeeded")
        self.assertEqual(target.read_text(encoding="utf-8"), "private note")
        repeated = self.tools.write_text(
            self.subject_id,
            target,
            "private note",
            idempotency_key="write-1",
        )
        self.assertEqual(repeated.action_id, written.action_id)
        self.assertTrue(
            self.capabilities.allows(
                self.subject_id,
                "filesystem_read",
                str(target),
                side_effect=False,
            )
        )
        read = self.tools.read_text(self.subject_id, target, idempotency_key="read-1")
        self.assertEqual(read.content, "private note")
        self.assertFalse(
            self.capabilities.allows(
                self.subject_id,
                "filesystem_read",
                str(target),
                side_effect=False,
            )
        )
        with self.assertRaises(CapabilityDeniedError):
            self.tools.read_text(self.subject_id, target, idempotency_key="read-2")
        counts = CapabilityIntegrity(self.kernel.database).verify(self.subject_id)
        self.assertEqual(counts["capability_uses"], 2)

    def test_legacy_approval_flag_fails_closed_for_any_token(self) -> None:
        proposal = CapabilityGrant(
            capability_type="filesystem_write",
            scope={"root": str(self.root)},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=True,
        )
        record = self.capabilities.grant(self.subject_id, proposal, actor="operator")
        legacy = proposal.model_copy(update={"requires_approval": True})
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                (record.grant_id,),
            ).fetchone()
            assert row is not None
            state_hash = self.capabilities._state_hash_values(
                self.subject_id,
                legacy,
                "active",
                row["created_at"],
                None,
                None,
            )
            connection.execute(
                "UPDATE capability_grants SET requires_approval = 1, state_hash = ? "
                "WHERE grant_id = ?",
                (state_hash, record.grant_id),
            )
        target = self.root / "legacy.txt"
        self.assertFalse(
            self.capabilities.allows(
                self.subject_id,
                "filesystem_write",
                str(target),
                side_effect=True,
                approval_id="arbitrary-token",
            )
        )
        with self.assertRaises(CapabilityDeniedError):
            self.tools.write_text(
                self.subject_id,
                target,
                "must not write",
                approval_id="arbitrary-token",
                idempotency_key="legacy-approval-attempt",
            )
        self.assertFalse(target.exists())
        self.assertEqual(
            CapabilityIntegrity(self.kernel.database).verify(self.subject_id)["capability_uses"],
            0,
        )

    def test_per_use_approval_is_not_advertised_by_configuration_ui(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "src" / "noyra" / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (root / "src" / "noyra" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("capability-approval", html)
        self.assertNotIn("requires_approval: document", javascript)

    def test_scope_escape_and_revoked_grant_are_denied(self) -> None:
        with self.assertRaises(ValueError):
            self.capabilities.grant(
                self.subject_id,
                CapabilityGrant(
                    capability_type="filesystem_read",
                    scope={"root": "relative/path"},
                    issuer="workspace-owner",
                    rate_limit_per_hour=10,
                    side_effect=False,
                ),
                actor="operator",
            )
        grant = self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="filesystem_read",
                scope={"root": str(self.root)},
                issuer="workspace-owner",
                rate_limit_per_hour=10,
                side_effect=False,
            ),
            actor="operator",
        )
        missing = self.tools.read_text(self.subject_id, self.root / "missing.txt")
        self.assertEqual(missing.status, "failed")
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(CapabilityDeniedError):
            self.tools.read_text(self.subject_id, outside)
        self.capabilities.revoke(
            grant.grant_id,
            reason="access withdrawn",
            actor="operator",
            subject_id=self.subject_id,
        )
        inside = self.root / "inside.txt"
        inside.write_text("inside", encoding="utf-8")
        with self.assertRaises(CapabilityDeniedError):
            self.tools.read_text(self.subject_id, inside)
        self.assertEqual(
            CapabilityIntegrity(self.kernel.database).verify(self.subject_id)["capability_grants"],
            1,
        )

    def test_autonomy_loop_heartbeats_and_requests_sleep_without_busy_work(self) -> None:
        hook_calls: list[str] = []

        async def active_hook() -> str:
            hook_calls.append("tick")
            return "observed"

        loop = AutonomyLoop(
            self.kernel,
            config=LoopConfig(active_interval_seconds=1, sleep_interval_seconds=2),
            active_hook=active_hook,
        )
        result = asyncio.run(loop.tick())
        self.assertEqual(result.action, "observed")
        self.assertEqual(hook_calls, ["tick"])
        self.assertIsNotNone(result.event_id)
        FatigueTracker(self.kernel.database).assess(
            self.subject_id,
            FatigueInputs(
                resource_pressure=1.0,
                cognitive_load=1.0,
                frustration=1.0,
                goal_conflict=1.0,
                staleness=1.0,
            ),
            reason="budget exhausted",
        )
        sleeping = asyncio.run(loop.tick())
        self.assertEqual(sleeping.action, "sleep_requested")
        self.assertEqual(self.kernel.lifecycle.current().state, "winding_down")
        waiting = asyncio.run(loop.tick())
        self.assertEqual(waiting.action, "sleep_state_wait")

        async def run_briefly() -> None:
            stop = asyncio.Event()

            async def stop_soon() -> None:
                await asyncio.sleep(0.01)
                stop.set()

            stopper = asyncio.create_task(stop_soon())
            await loop.run_forever(stop)
            await stopper

        asyncio.run(run_briefly())

    def test_autonomy_loop_opens_circuit_after_repeated_failures(self) -> None:
        async def failing_hook() -> str:
            raise RuntimeError("persistent failure")

        loop = AutonomyLoop(
            self.kernel,
            config=LoopConfig(
                active_interval_seconds=1,
                max_consecutive_failures=2,
                circuit_cooldown_seconds=60,
            ),
            active_hook=failing_hook,
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(loop.tick())
        with self.assertRaises(RuntimeError):
            asyncio.run(loop.tick())
        self.assertEqual(asyncio.run(loop.tick()).action, "circuit_open")
        self.assertEqual(loop.health()["circuit_status"], "open")

    def test_durable_workflow_requires_valid_resume_transitions(self) -> None:
        workflows = DurableWorkflowStore(self.kernel.database)
        first = workflows.checkpoint(
            self.subject_id,
            "workflow-test",
            "research",
            "running",
            {"step": 1},
            reason="workflow started",
        )
        interrupted = workflows.checkpoint(
            self.subject_id,
            "workflow-test",
            "research",
            "interrupted",
            {"step": 1, "pending": "network"},
            reason="provider unavailable",
        )
        resumed = workflows.resume(
            self.subject_id,
            "workflow-test",
            {"step": 2},
            reason="provider recovered",
        )
        self.assertEqual((first.checkpoint_version, interrupted.status), (1, "interrupted"))
        self.assertEqual(resumed.checkpoint_version, 3)

    def test_public_web_read_requires_host_scoped_grant(self) -> None:
        source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Example",
            "https://example.com/news",
            "news",
            status="active",
            reason="test source",
        )
        self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="web_read",
                scope={"hosts": ["example.com"]},
                issuer="workspace-owner",
                rate_limit_per_hour=5,
                side_effect=False,
            ),
            actor="operator",
        )

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="authorized public observation",
            )

        async def resolver(host: str, port: int) -> tuple[str, ...]:
            del host, port
            return ("93.184.216.34",)

        async def execute() -> str | None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
            try:
                result = await self.tools.fetch_public(
                    self.subject_id, source, reader, idempotency_key="web-read-1"
                )
                return result.content
            finally:
                await client.aclose()

        self.assertEqual(asyncio.run(execute()), "authorized public observation")

    def test_public_web_read_policy_covers_new_public_hosts_but_not_private_urls(self) -> None:
        self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="web_read",
                scope={"public_https": True},
                issuer="environment-config",
                rate_limit_per_hour=5,
                side_effect=False,
            ),
            actor="operator",
        )
        self.assertTrue(
            self.capabilities.allows(
                self.subject_id,
                "web_read",
                "https://new-public.example/article?view=1",
                side_effect=False,
            )
        )
        for url in (
            "http://new-public.example/article",
            "https://127.0.0.1/private",
            "https://new-public.example:8443/article",
            "https://user:password@new-public.example/article",
        ):
            self.assertFalse(
                self.capabilities.allows(
                    self.subject_id,
                    "web_read",
                    url,
                    side_effect=False,
                ),
                url,
            )
        self.assertEqual(
            CapabilityIntegrity(self.kernel.database).verify(self.subject_id)["capability_grants"],
            1,
        )

    def test_web_read_rejects_wildcard_host_scopes(self) -> None:
        with self.assertRaisesRegex(ValueError, "wildcard hosts"):
            self.capabilities.grant(
                self.subject_id,
                CapabilityGrant(
                    capability_type="web_read",
                    scope={"hosts": ["*"]},
                    issuer="workspace-owner",
                    rate_limit_per_hour=5,
                    side_effect=False,
                ),
                actor="operator",
            )

    def test_legacy_wildcard_web_read_grant_fails_closed(self) -> None:
        proposal = CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="workspace-owner",
            rate_limit_per_hour=5,
            side_effect=False,
        )
        record = self.capabilities.grant(self.subject_id, proposal, actor="operator")
        legacy = proposal.model_copy(update={"scope": {"hosts": ["*"]}})
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                (record.grant_id,),
            ).fetchone()
            assert row is not None
            state_hash = self.capabilities._state_hash_values(
                self.subject_id,
                legacy,
                "active",
                row["created_at"],
                None,
                None,
            )
            connection.execute(
                "UPDATE capability_grants SET scope_json = ?, state_hash = ? WHERE grant_id = ?",
                (canonical_json(legacy.scope), state_hash, record.grant_id),
            )
        self.assertFalse(
            self.capabilities.allows(
                self.subject_id,
                "web_read",
                "https://new-public.example/article",
                side_effect=False,
            )
        )
        self.assertEqual(
            CapabilityIntegrity(self.kernel.database).verify(self.subject_id)["capability_grants"],
            1,
        )

    def test_web_read_malformed_urls_fail_closed(self) -> None:
        self.capabilities.grant(
            self.subject_id,
            CapabilityGrant(
                capability_type="web_read",
                scope={"public_https": True},
                issuer="workspace-owner",
                rate_limit_per_hour=10,
                side_effect=False,
            ),
            actor="operator",
        )

        for url in ("https://[bad", "https://example.com:bad/path"):
            with self.subTest(url=url):
                self.assertFalse(
                    self.capabilities.allows(
                        self.subject_id,
                        "web_read",
                        url,
                        side_effect=False,
                    )
                )
                with self.assertRaises(CapabilityDeniedError):
                    self.capabilities.use(
                        self.subject_id,
                        "web_read",
                        url,
                        side_effect=False,
                    )

    def test_persisted_malformed_scopes_raise_integrity_error_even_with_matching_hash(
        self,
    ) -> None:
        cases: tuple[tuple[str, CapabilityType, dict[str, object], dict[str, object]], ...] = (
            (
                "web_read hosts with non-string entry",
                "web_read",
                {"hosts": ["example.com"]},
                {"hosts": [1]},
            ),
            (
                "web_read public_https with non-boolean entry",
                "web_read",
                {"public_https": True},
                {"public_https": "true"},
            ),
            (
                "web_read with empty hosts",
                "web_read",
                {"hosts": ["example.com"]},
                {"hosts": []},
            ),
            (
                "filesystem with relative root",
                "filesystem_read",
                {"root": str(self.root)},
                {"root": "relative/path"},
            ),
            (
                "message with non-string exact resource",
                "message",
                {"exact": ["operator"]},
                {"exact": [1]},
            ),
        )

        for label, capability_type, valid_scope, malformed_scope in cases:
            with self.subTest(label=label):
                proposal = CapabilityGrant(
                    capability_type=capability_type,
                    scope=valid_scope,
                    issuer="workspace-owner",
                    rate_limit_per_hour=10,
                    side_effect=capability_type in {"message", "publish", "wallet"},
                )
                record = self.capabilities.grant(
                    self.subject_id,
                    proposal,
                    actor="operator",
                )
                with self.kernel.database.transaction() as connection:
                    row = connection.execute(
                        "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                        (record.grant_id,),
                    ).fetchone()
                    assert row is not None
                    malformed = proposal.model_copy(update={"scope": malformed_scope})
                    malformed_hash = self.capabilities._state_hash_values(
                        self.subject_id,
                        malformed,
                        "active",
                        row["created_at"],
                        None,
                        None,
                    )
                    connection.execute(
                        "UPDATE capability_grants SET scope_json = ?, state_hash = ? "
                        "WHERE grant_id = ?",
                        (canonical_json(malformed_scope), malformed_hash, record.grant_id),
                    )

                try:
                    with self.assertRaises(IntegrityError):
                        CapabilityIntegrity(self.kernel.database).verify(self.subject_id)
                finally:
                    with self.kernel.database.transaction() as connection:
                        row = connection.execute(
                            "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                            (record.grant_id,),
                        ).fetchone()
                        assert row is not None
                        valid_hash = self.capabilities._state_hash_values(
                            self.subject_id,
                            proposal,
                            "active",
                            row["created_at"],
                            None,
                            None,
                        )
                        connection.execute(
                            "UPDATE capability_grants SET scope_json = ?, state_hash = ? "
                            "WHERE grant_id = ?",
                            (canonical_json(valid_scope), valid_hash, record.grant_id),
                        )

    def test_allows_rejects_noncanonical_persisted_scalar_with_matching_hash(self) -> None:
        proposal = CapabilityGrant(
            capability_type="web_read",
            scope={"hosts": ["example.com"]},
            issuer="workspace-owner",
            rate_limit_per_hour=10,
            side_effect=False,
        )
        record = self.capabilities.grant(
            self.subject_id,
            proposal,
            actor="operator",
        )
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT created_at FROM capability_grants WHERE grant_id = ?",
                (record.grant_id,),
            ).fetchone()
            assert row is not None
            malformed_state = proposal.model_dump(mode="json")
            malformed_state["rate_limit_per_hour"] = 10.5
            malformed_hash = content_hash(
                {
                    "subject_id": self.subject_id,
                    **malformed_state,
                    "status": "active",
                    "created_at": row["created_at"],
                    "revoked_at": None,
                    "revoke_reason": None,
                }
            )
            connection.execute(
                "UPDATE capability_grants SET rate_limit_per_hour = ?, state_hash = ? "
                "WHERE grant_id = ?",
                (10.5, malformed_hash, record.grant_id),
            )

        with self.assertRaises(IntegrityError):
            self.capabilities.allows(
                self.subject_id,
                "web_read",
                "https://example.com/article",
                side_effect=False,
            )


if __name__ == "__main__":
    unittest.main()
