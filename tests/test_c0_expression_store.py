from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from noyra.capability import CapabilityStore
from noyra.capability.errors import CapabilityDeniedError
from noyra.core import SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.interaction import DeliveryDispatcher, TransportInput, TransportStore
from noyra.interaction.expression import ExpressionProposal, ExpressionReview, ExpressionStore
from noyra.interaction.public_archive import PublicArchive
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)


class ExpressionStoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.subject = "c0-expression"
        self.kernel = SubjectKernel(self.root / "noyra.sqlite3", self.subject, content_hash("c0"))
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.db = self.kernel.database
        self.store = ExpressionStore(self.db, self.subject)
        self.store.ensure_grants(enabled=True, rate=20)
        self.transports = TransportStore(self.db, self.root / "secrets")
        self.sequence = 0

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    async def review(self, action: str, *, approved: bool = True) -> str:
        provider = FakeProvider(
            [
                ProviderResponse(
                    json.dumps({"approved": approved, "reason": "Self review"}), ModelUsage(10, 10)
                )
            ]
        )
        gateway = ModelGateway(
            provider,
            ModelLedger(self.db),
            model="fixture",
            limits=BudgetLimits(100, 500_000, 100_000, 5_000_000),
        )
        from noyra.model import ModelMessage

        result = await gateway.complete_structured(
            self.subject,
            "expression_review:" + action,
            (ModelMessage(role="user", content="Synthetic public draft"),),
            ExpressionReview,
            idempotency_key="expression-review:" + action,
        )
        return result.call_id

    def article(self, **updates: object) -> ExpressionProposal:
        return ExpressionProposal.model_validate(
            {
                "operation": "write",
                "title": "A question worth exploring",
                "summary": "An observation invited another question.",
                "body": "I want to compare what different sources report.",
                "reason": "An autonomous expression",
                **updates,
            }
        )

    def prepare(self, proposal: ExpressionProposal) -> str:
        self.sequence += 1
        return self.store.prepare(proposal, "fixture-planner-" + str(self.sequence)).action_id

    async def test_publish_revise_withdraw_and_actual_receipt_without_human_approval(self) -> None:
        proposal = self.article()
        action = self.prepare(proposal)
        review = await self.review(action)
        receipt = self.store.execute(action, proposal, review_call_id=review)
        self.assertEqual(receipt["status"], "published")
        public = PublicArchive(self.db).admin_item(self.subject, receipt["publication_id"])
        assert public is not None
        self.assertEqual(public["provenance"], "subject")
        self.assertEqual(self.store.execute(action, proposal, review_call_id=review), receipt)
        revised = self.article(
            operation="revise",
            publication_id=receipt["publication_id"],
            expected_revision=1,
            body="I revised my view after comparing evidence.",
        )
        action = self.prepare(revised)
        receipt = self.store.execute(action, revised, review_call_id=await self.review(action))
        self.assertEqual(receipt["revision"], 2)
        withdraw = ExpressionProposal(
            operation="withdraw",
            publication_id=receipt["publication_id"],
            expected_revision=2,
            reason="I choose to withdraw this text",
        )
        action = self.prepare(withdraw)
        self.assertEqual(
            self.store.execute(action, withdraw, review_call_id=await self.review(action))[
                "status"
            ],
            "withdrawn",
        )
        self.assertEqual(
            (PublicArchive(self.db).admin_item(self.subject, receipt["publication_id"]) or {})[
                "status"
            ],
            "withdrawn",
        )
        ActionLedger(self.db).verify_integrity(self.subject)

    async def test_revoked_publication_grant_is_not_recreated(self) -> None:
        grant = next(
            item
            for item in CapabilityStore(self.db).list(self.subject)
            if item.capability_type == "publish"
        )
        CapabilityStore(self.db).revoke(
            grant.grant_id, subject_id=self.subject, actor="operator", reason="stop"
        )
        self.store.ensure_grants(enabled=True, rate=20)
        proposal = self.article()
        action = self.prepare(proposal)
        with self.assertRaises(CapabilityDeniedError):
            self.store.execute(action, proposal, review_call_id=await self.review(action))
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    async def test_failed_commit_rolls_back_publication_and_action_settlement(self) -> None:
        proposal = self.article()
        action = self.prepare(proposal)

        def fail() -> None:
            raise RuntimeError("fixture interruption")

        with self.assertRaises(RuntimeError):
            self.store.execute(
                action, proposal, review_call_id=await self.review(action), before_commit=fail
            )
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])
        with self.db.connection() as c:
            self.assertEqual(
                c.execute("SELECT status FROM actions WHERE action_id=?", (action,)).fetchone()[0],
                "failed",
            )

    async def test_rejected_self_review_cannot_publish(self) -> None:
        proposal = self.article()
        action = self.prepare(proposal)
        with self.assertRaises(PermissionError):
            self.store.execute(
                action, proposal, review_call_id=await self.review(action, approved=False)
            )
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    async def test_operator_article_cannot_be_edited_as_subject(self) -> None:
        archive = PublicArchive(self.db)
        archive.publish(
            self.subject,
            {
                "publication_id": "expression_operator",
                "kind": "update",
                "title": "Operator note",
                "summary": "Operator summary",
                "body": "Reviewed by operator",
                "details": {},
                "expected_revision": 0,
                "confirm_public": True,
            },
            actor="operator",
        )
        proposal = self.article(
            operation="revise", publication_id="expression_operator", expected_revision=1
        )
        action = self.prepare(proposal)
        with self.assertRaises(PermissionError):
            self.store.execute(action, proposal, review_call_id=await self.review(action))
        self.assertEqual(
            (archive.admin_item(self.subject, "expression_operator") or {})["title"],
            "Operator note",
        )

    def test_ordinary_public_entry_cannot_impersonate_subject(self) -> None:
        payload = {
            "publication_id": "expression_spoof",
            "kind": "update",
            "title": "Title",
            "summary": "Summary",
            "body": "Body",
            "details": {"status": "subject_article"},
            "expected_revision": 0,
            "confirm_public": True,
        }
        with self.assertRaises(PermissionError):
            PublicArchive(self.db).publish(self.subject, payload, actor="subject")
        with self.assertRaises(ValueError):
            PublicArchive(self.db).publish(self.subject, payload, actor="operator")

    def test_secret_material_cannot_enter_public_draft(self) -> None:
        with self.assertRaises(ValueError):
            self.prepare(self.article(body="Authorization: Bearer synthetic-secret-token"))

    async def test_message_uses_frozen_transport_and_revoke_stops_delivery(self) -> None:
        first = self.transports.configure(
            self.subject,
            TransportInput(channel="webhook", label="first", endpoint="https://first.example/send"),
            actor="operator",
        )
        second = self.transports.configure(
            self.subject,
            TransportInput(
                channel="webhook", label="second", endpoint="https://second.example/send"
            ),
            actor="operator",
        )
        self.store.ensure_grants(enabled=True, rate=20)
        proposal = ExpressionProposal(
            operation="message",
            body="A public question I want to share.",
            recipient="another-subject",
            transport_id=second.transport_id,
            reason="Start a conversation",
        )
        action = self.prepare(proposal)
        receipt = self.store.execute(action, proposal, review_call_id=await self.review(action))
        self.assertEqual(receipt["status"], "queued")
        calls: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            dispatcher.enqueue_pending(self.subject)
            with self.db.connection() as c:
                delivery = c.execute("SELECT * FROM interaction_deliveries").fetchone()
            self.assertEqual(delivery["transport_id"], second.transport_id)
            grant = next(
                item
                for item in CapabilityStore(self.db).list(self.subject)
                if item.scope == {"exact": ["transport:" + second.transport_id]}
            )
            CapabilityStore(self.db).revoke(
                grant.grant_id, actor="operator", reason="stop", subject_id=self.subject
            )
            results = await dispatcher.deliver_pending(self.subject)
            self.assertEqual(calls, [])
            self.assertTrue(all(item.status != "delivered" for item in results))
            await dispatcher.aclose()
        self.assertNotEqual(first.transport_id, second.transport_id)
