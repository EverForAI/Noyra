from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from noyra.capability import CapabilityStore
from noyra.capability.errors import CapabilityDeniedError
from noyra.core import Database, EventStore, SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash
from noyra.interaction import (
    DeliveryDispatcher,
    InboundEnvelope,
    InboundStore,
    InteractionStore,
    TransportInput,
    TransportRecord,
    TransportStore,
)
from noyra.interaction.expression import ExpressionProposal, ExpressionReview, ExpressionStore
from noyra.interaction.participants import ParticipantStore
from noyra.interaction.public_archive import PublicArchive
from noyra.interaction.types import InteractionDecision
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
        self.assertEqual(self.store.verify_integrity(), 3)

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

    async def test_fixed_route_message_really_delivers_once_and_unknown_is_not_retried(
        self,
    ) -> None:
        transport = self.transports.configure(
            self.subject,
            TransportInput(
                channel="webhook", label="contact", endpoint="https://contact.example/send"
            ),
            actor="operator",
        )
        self.store.ensure_grants(enabled=True, rate=20)
        proposal = ExpressionProposal(
            operation="message",
            body="A question I chose to share.",
            recipient="target-subject",
            transport_id=transport.transport_id,
            reason="Conversation",
        )
        action = self.prepare(proposal)
        self.store.execute(action, proposal, review_call_id=await self.review(action))
        calls: list[str] = []

        async def success(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True, "message_id": "remote-one"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            delivered = await dispatcher.deliver_pending(self.subject)
            self.assertEqual(delivered[0].status, "delivered")
            self.assertEqual(await dispatcher.deliver_pending(self.subject), [])
            self.assertEqual(len(calls), 1)
            await dispatcher.aclose()
        second = proposal.model_copy(update={"body": "Another question with its own receipt."})
        action = self.prepare(second)
        self.store.execute(action, second, review_call_id=await self.review(action))

        async def unknown(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            raise httpx.ReadTimeout("response lost", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(unknown)) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            results = await dispatcher.deliver_pending(self.subject)
            self.assertEqual(results[0].status, "unknown")
            self.assertEqual(await dispatcher.deliver_pending(self.subject), [])
            self.assertEqual(len(calls), 2)
            await dispatcher.aclose()

    async def test_publication_rechecks_revocation_inside_transaction(self) -> None:
        proposal = self.article()
        action = self.prepare(proposal)
        review = await self.review(action)
        grant = next(
            item
            for item in CapabilityStore(self.db).list(self.subject)
            if item.capability_type == "publish"
        )
        # Withdraw before transaction admission; no statement may publish it.
        CapabilityStore(self.db).revoke(
            grant.grant_id, actor="operator", reason="stop", subject_id=self.subject
        )
        with self.assertRaises(CapabilityDeniedError):
            self.store.execute(action, proposal, review_call_id=review)
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    async def test_wrong_action_review_does_not_authorize_a_different_draft(self) -> None:
        first = self.article()
        one = self.prepare(first)
        review = await self.review(one)
        second = self.article(body="Different draft with a different action.")
        two = self.prepare(second)
        with self.assertRaises(PermissionError):
            self.store.execute(two, second, review_call_id=review)
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    def test_configured_disable_and_reenable_preserve_manual_revocation(self) -> None:
        self.store.ensure_grants(enabled=False, rate=20)
        self.assertFalse(
            CapabilityStore(self.db).allows(
                self.subject, "publish", "instance:" + self.subject, side_effect=True
            )
        )
        self.store.ensure_grants(enabled=True, rate=10)
        self.assertTrue(
            CapabilityStore(self.db).allows(
                self.subject, "publish", "instance:" + self.subject, side_effect=True
            )
        )

    async def test_integrity_rejects_published_effect_with_a_contradictory_self_review(
        self,
    ) -> None:
        from noyra.core.errors import IntegrityError
        from noyra.core.types import canonical_json

        proposal = self.article()
        action = self.prepare(proposal)
        review = await self.review(action)
        self.store.execute(action, proposal, review_call_id=review)
        call = ModelLedger(self.db).get_call(review)
        response = dict(call.response or {})
        response["content"] = json.dumps({"approved": False, "reason": "Changed evidence"})
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET response_json=?,response_hash=? WHERE call_id=?",
                (canonical_json(response), content_hash(response), review),
            )
        with self.assertRaises(IntegrityError):
            self.store.verify_integrity()

    async def test_lost_commit_response_recovers_atomic_publication_receipt(self) -> None:
        from collections.abc import Iterator
        from contextlib import contextmanager
        from unittest.mock import patch

        proposal = self.article()
        action = self.prepare(proposal)
        review = await self.review(action)
        original = self.db.transaction

        @contextmanager
        def lost_response() -> Iterator[Any]:
            committed = False
            with original() as connection:
                yield connection
                state = connection.execute(
                    "SELECT status FROM actions WHERE action_id=?", (action,)
                ).fetchone()
                committed = state is not None and state[0] == "succeeded"
            if committed:
                raise RuntimeError("response lost after local commit")

        with patch.object(self.db, "transaction", lost_response):
            receipt = self.store.execute(action, proposal, review_call_id=review)
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(len(PublicArchive(self.db).admin(self.subject)), 1)
        self.assertEqual(self.store.execute(action, proposal, review_call_id=review), receipt)
        self.assertEqual(self.store.verify_integrity(), 1)

    async def test_authenticated_reply_returns_to_exact_proactive_message(self) -> None:
        from noyra.interaction import InboundEnvelope, InboundStore, InteractionStore
        from noyra.interaction.participants import ParticipantStore
        from noyra.interaction.types import InteractionDecision

        transport = self.transports.configure(
            self.subject,
            TransportInput(
                channel="webhook",
                label="conversation",
                endpoint="https://conversation.example/send",
                settings={"account_id": "dedicated-account"},
            ),
            actor="operator",
        )
        self.store.ensure_grants(enabled=True, rate=20)
        proposal = ExpressionProposal(
            operation="message",
            body="A public question for you.",
            recipient="correspondent",
            transport_id=transport.transport_id,
            reason="Discussion",
        )
        action = self.prepare(proposal)
        sent = self.store.execute(action, proposal, review_call_id=await self.review(action))
        provider_message_ids = iter(("remote-c0", "remote-c1"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"ok": True, "message_id": next(provider_message_ids)}
                )
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            self.assertEqual(
                (await dispatcher.deliver_pending(self.subject))[0].status, "delivered"
            )
            inbound = InboundStore(self.db)
            reply = inbound.ingest(
                InboundEnvelope(
                    channel="webhook",
                    transport_id=transport.transport_id,
                    provider_event_id="response-one",
                    external_account_id="dedicated-account",
                    external_sender_id="correspondent",
                    conversation_id="correspondent",
                    thread_id="remote-c0",
                    content="Here is a suggestion for your question.",
                )
            )
            with self.db.connection() as connection:
                linked = connection.execute(
                    "SELECT related_interaction_id FROM interactions WHERE interaction_id=?",
                    (reply.interaction_id,),
                ).fetchone()[0]
            self.assertEqual(linked, sent["interaction_id"])
            history = ParticipantStore(self.db).history(self.subject, reply.interaction_id)
            self.assertEqual([item["content"] for item in history], [proposal.body])
            InteractionStore(self.db).decide(
                reply.interaction_id,
                InteractionDecision(disposition="accepted", rationale="Continue the discussion"),
            )
            followup = InteractionStore(self.db).send(
                self.subject,
                "webhook",
                "correspondent",
                "I compared the source. What do you think of this follow-up?",
                related_interaction_id=reply.interaction_id,
                idempotency_key="ordinary-reply-two",
            )
            deliveries = await dispatcher.deliver_pending(self.subject)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0].status, "delivered")
            second = inbound.ingest(
                InboundEnvelope(
                    channel="webhook",
                    transport_id=transport.transport_id,
                    provider_event_id="response-two",
                    external_account_id="dedicated-account",
                    external_sender_id="correspondent",
                    conversation_id="correspondent",
                    thread_id="remote-c1",
                    content="The follow-up supports comparing independent reports.",
                )
            )
            with self.db.connection() as connection:
                second_parent = connection.execute(
                    "SELECT related_interaction_id FROM interactions WHERE interaction_id=?",
                    (second.interaction_id,),
                ).fetchone()[0]
            self.assertEqual(second_parent, followup.interaction_id)
            second_history = ParticipantStore(self.db).history(self.subject, second.interaction_id)
            self.assertEqual(
                [item["content"] for item in second_history],
                [
                    proposal.body,
                    "Here is a suggestion for your question.",
                    "I compared the source. What do you think of this follow-up?",
                ],
            )
            await dispatcher.aclose()
        wrong = inbound.ingest(
            InboundEnvelope(
                channel="webhook",
                transport_id=transport.transport_id,
                provider_event_id="wrong-recipient",
                external_account_id="dedicated-account",
                external_sender_id="someone-else",
                conversation_id="someone-else",
                thread_id="remote-c0",
                content="Guessing another thread should reveal nothing.",
            )
        )
        with self.db.connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT related_interaction_id FROM interactions WHERE interaction_id=?",
                    (wrong.interaction_id,),
                ).fetchone()[0]
            )
        self.assertEqual(ParticipantStore(self.db).history(self.subject, wrong.interaction_id), [])

    async def test_help_is_not_sent_to_an_arbitrary_configured_account(self) -> None:
        from noyra.interaction import InteractionStore

        unrelated = self.transports.configure(
            self.subject,
            TransportInput(
                channel="webhook", label="unrelated", endpoint="https://unrelated.example/send"
            ),
            actor="operator",
        )
        InteractionStore(self.db).send(
            self.subject,
            "web",
            "web-user",
            "Please help with a public question.",
            kind="help_request",
            idempotency_key="no-help-recipient",
        )
        calls: list[str] = []

        async def response(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            self.assertEqual(await dispatcher.deliver_pending(self.subject), [])
            self.assertEqual(calls, [])
            configured = self.transports.configure(
                self.subject,
                TransportInput(
                    channel="webhook",
                    label="creator",
                    endpoint="https://creator.example/send",
                    settings={"recipient": "creator"},
                ),
                actor="operator",
            )
            results = await dispatcher.deliver_pending(self.subject)
            self.assertEqual(results[0].transport_id, configured.transport_id)
            self.assertNotEqual(results[0].transport_id, unrelated.transport_id)
            self.assertEqual(calls, ["https://creator.example/send"])
            await dispatcher.aclose()

    async def _direct_contact(
        self, channel: Literal["email", "webhook"] = "webhook"
    ) -> tuple[TransportRecord, dict[str, Any]]:
        transport = self.transports.configure(
            self.subject,
            TransportInput(
                channel=channel,
                label="AR03 direct contact",
                endpoint="smtp://mail.example:587"
                if channel == "email"
                else "https://contact.example/send",
                settings={"account_id": "noyra@example.com"},
                credentials={"from_address": SecretStr("noyra@example.com")}
                if channel == "email"
                else {},
            ),
            actor="operator",
        )
        self.store.ensure_grants(enabled=True, rate=20)
        proposal = ExpressionProposal(
            operation="message",
            body="An initial public question.",
            transport_id=transport.transport_id,
            recipient="helper@example.com",
            reason="A subject-chosen discussion",
        )
        action = self.prepare(proposal)
        receipt = self.store.execute(action, proposal, review_call_id=await self.review(action))
        return transport, receipt

    @staticmethod
    def _direct_reply(transport: TransportRecord, reference: str, number: int) -> InboundEnvelope:
        assert transport.channel in {"email", "webhook"}
        return InboundEnvelope(
            channel=cast(Literal["email", "webhook"], transport.channel),
            transport_id=transport.transport_id,
            provider_event_id=f"<incoming-{number}@helper.example>",
            external_account_id="noyra@example.com",
            external_sender_id="helper@example.com",
            conversation_id=reference if transport.channel == "email" else "helper@example.com",
            thread_id=reference,
            content=f"Public answer number {number}.",
        )

    async def test_email_three_rounds_keep_private_history_and_reply_headers_after_reopen(
        self,
    ) -> None:
        messages: list[EmailMessage] = []

        class SMTP:
            sock = None

            def __init__(self, **kwargs: Any) -> None:
                pass

            def connect(self, host: str, port: int) -> tuple[int, bytes]:
                return 220, b"ready"

            def starttls(self, **kwargs: Any) -> None:
                pass

            def send_message(self, message: EmailMessage) -> dict[str, Any]:
                messages.append(message)
                return {}

            def close(self) -> None:
                pass

        transport, receipt = await self._direct_contact("email")
        expected = ["An initial public question."]
        parent = receipt["interaction_id"]
        with (
            patch("noyra.interaction.transport.smtplib.SMTP", SMTP),
            patch("noyra.interaction.transport.public_addresses", return_value=["8.8.8.8"]),
            patch.object(DeliveryDispatcher, "_verify_smtp_peer"),
        ):
            dispatcher = DeliveryDispatcher(self.db, self.transports)
            try:
                for number in range(1, 4):
                    delivered = await dispatcher.deliver_pending(self.subject)
                    self.assertEqual(len(delivered), 1)
                    self.assertEqual(delivered[0].status, "delivered")
                    reference = str(messages[-1]["Message-ID"])
                    self.assertEqual(reference, delivered[0].provider_message_id)
                    if number == 1:
                        self.assertIsNone(messages[-1]["In-Reply-To"])
                    else:
                        incoming_reference = f"<incoming-{number - 1}@helper.example>"
                        self.assertEqual(str(messages[-1]["In-Reply-To"]), incoming_reference)
                        self.assertEqual(str(messages[-1]["References"]), incoming_reference)
                    reopened = Database(self.db.path, initialize=False)
                    inbound = InboundStore(reopened)
                    envelope = self._direct_reply(transport, reference, number)
                    reply = inbound.ingest(envelope)
                    duplicate = InboundStore(reopened).ingest(envelope)
                    self.assertEqual(reply.interaction_id, duplicate.interaction_id)
                    self.assertTrue(duplicate.duplicate)
                    item = InteractionStore(reopened).get(reply.interaction_id)
                    self.assertEqual(item.related_interaction_id, parent)
                    self.assertEqual(item.status, "offered")
                    history = ParticipantStore(reopened).history(self.subject, reply.interaction_id)
                    self.assertEqual([row["content"] for row in history], expected)
                    expected.append(envelope.content)
                    if number < 3:
                        InteractionStore(reopened).decide(
                            reply.interaction_id,
                            InteractionDecision(disposition="accepted", rationale="Keep talking"),
                        )
                        response = f"Follow-up number {number}."
                        sent = InteractionStore(reopened).send(
                            self.subject,
                            "email",
                            "helper@example.com",
                            response,
                            related_interaction_id=reply.interaction_id,
                            idempotency_key=f"ar03-email-followup:{number}",
                        )
                        parent = sent.interaction_id
                        expected.append(response)
                self.assertEqual(await dispatcher.deliver_pending(self.subject), [])
            finally:
                await dispatcher.aclose()
        with self.db.connection() as connection:
            keys = connection.execute(
                "SELECT DISTINCT conversation_key,visibility FROM participant_message_links "
                "WHERE subject_id=?",
                (self.subject,),
            ).fetchall()
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["visibility"], "private")
        from noyra.interaction.integrity import InteractionIntegrity

        InteractionIntegrity(self.db).verify(self.subject)
        EventStore(self.db).verify_chain(self.subject)

    async def test_followup_references_do_not_share_history_across_identity_boundaries(
        self,
    ) -> None:
        transport, _ = await self._direct_contact()
        references = iter(("ar03-root", "ar03-followup", "ar03-followup"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message_id": next(references)})
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            await dispatcher.deliver_pending(self.subject)
            inbound = InboundStore(self.db)
            first = inbound.ingest(self._direct_reply(transport, "ar03-root", 1))
            followup = InteractionStore(self.db).send(
                self.subject,
                "webhook",
                "helper@example.com",
                "This belongs to the established private thread.",
                related_interaction_id=first.interaction_id,
                idempotency_key="ar03-private-followup",
            )
            await dispatcher.deliver_pending(self.subject)
            alternate = self.transports.configure(
                self.subject,
                TransportInput(
                    channel="webhook",
                    label="Different sending account",
                    endpoint="https://other-contact.example/send",
                    settings={"account_id": "noyra@example.com"},
                ),
                actor="operator",
            )
            variants: tuple[dict[str, Any], ...] = (
                {"external_account_id": "other-account@example.com"},
                {
                    "external_sender_id": "stranger@example.com",
                    "conversation_id": "stranger@example.com",
                },
                {"transport_id": alternate.transport_id},
                {"thread_id": "unrelated-reference"},
                {"thread_id": None},
            )
            for number, changes in enumerate(variants, 10):
                with self.subTest(changes=changes):
                    envelope = self._direct_reply(transport, "ar03-followup", number).model_copy(
                        update=changes
                    )
                    received = inbound.ingest(envelope)
                    record = InteractionStore(self.db).get(received.interaction_id)
                    self.assertIsNone(record.related_interaction_id)
                    self.assertEqual(
                        ParticipantStore(self.db).history(self.subject, record.interaction_id), []
                    )
            # Ambiguity is a separate guard: the identity checks above used a
            # unique receipt so they cannot pass just because of this duplicate.
            InteractionStore(self.db).send(
                self.subject,
                "webhook",
                "helper@example.com",
                "Another message in this private thread.",
                related_interaction_id=first.interaction_id,
                idempotency_key="ar03-ambiguous-followup",
            )
            await dispatcher.deliver_pending(self.subject)
            ambiguous = inbound.ingest(self._direct_reply(transport, "ar03-followup", 20))
            ambiguous_record = InteractionStore(self.db).get(ambiguous.interaction_id)
            self.assertIsNone(ambiguous_record.related_interaction_id)
            self.assertEqual(
                ParticipantStore(self.db).history(self.subject, ambiguous.interaction_id), []
            )
            self.assertEqual(
                InteractionStore(self.db).get(followup.interaction_id).related_interaction_id,
                first.interaction_id,
            )
            await dispatcher.aclose()

    async def test_first_reply_participant_links_rollback_together_and_retry_cleanly(self) -> None:
        transport, receipt = await self._direct_contact()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message_id": "ar03-atomic-root"})
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            await dispatcher.deliver_pending(self.subject)
            await dispatcher.aclose()
        original_link = ParticipantStore.link

        def fail_root_link(
            connection: Any,
            subject: str,
            interaction: str,
            who: Any,
            conversation: str,
            *,
            private: bool = False,
        ) -> None:
            if interaction == receipt["interaction_id"]:
                raise RuntimeError("injected root link failure")
            original_link(connection, subject, interaction, who, conversation, private=private)

        envelope = self._direct_reply(transport, "ar03-atomic-root", 1)
        with (
            patch.object(ParticipantStore, "link", side_effect=fail_root_link),
            self.assertRaisesRegex(RuntimeError, "root link failure"),
        ):
            InboundStore(self.db).ingest(envelope)
        with self.db.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM interactions").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM participant_message_links").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT status FROM interaction_inbound_events").fetchone()[0],
                "rejected",
            )
        accepted = InboundStore(self.db).ingest(envelope)
        self.assertTrue(accepted.duplicate)
        self.assertEqual(
            [
                row["content"]
                for row in ParticipantStore(self.db).history(self.subject, accepted.interaction_id)
            ],
            ["An initial public question."],
        )

    async def test_parallel_first_replies_bind_one_private_conversation(self) -> None:
        transport, receipt = await self._direct_contact()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message_id": "ar03-parallel-root"})
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            await dispatcher.deliver_pending(self.subject)
            await dispatcher.aclose()
        envelopes = [self._direct_reply(transport, "ar03-parallel-root", n) for n in (1, 2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(InboundStore(self.db).ingest, envelopes))
        self.assertEqual(len({reply.interaction_id for reply in replies}), 2)
        with self.db.connection() as connection:
            links = connection.execute(
                "SELECT entity_id,source_id,conversation_key,visibility "
                "FROM participant_message_links WHERE subject_id=?",
                (self.subject,),
            ).fetchall()
            roots = connection.execute(
                "SELECT related_interaction_id FROM interactions WHERE direction='incoming'"
            ).fetchall()
        self.assertEqual(len(links), 3)
        self.assertEqual(len({tuple(row) for row in links}), 1)
        self.assertEqual({row[0] for row in roots}, {receipt["interaction_id"]})

    async def test_two_proactive_threads_with_one_correspondent_remain_separate(self) -> None:
        transport, first_receipt = await self._direct_contact()
        references = iter(("separate-root-1", "separate-root-2"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message_id": next(references)})
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            await dispatcher.deliver_pending(self.subject)
            first = InboundStore(self.db).ingest(
                self._direct_reply(transport, "separate-root-1", 1)
            )
            proposal = ExpressionProposal(
                operation="message",
                body="A separate public question for the same correspondent.",
                transport_id=transport.transport_id,
                recipient="helper@example.com",
                reason="A different discussion",
            )
            action = self.prepare(proposal)
            second_receipt = self.store.execute(
                action, proposal, review_call_id=await self.review(action)
            )
            await dispatcher.deliver_pending(self.subject)
            second = InboundStore(self.db).ingest(
                self._direct_reply(transport, "separate-root-2", 2)
            )
            self.assertEqual(
                [
                    item["content"]
                    for item in ParticipantStore(self.db).history(
                        self.subject, first.interaction_id
                    )
                ],
                ["An initial public question."],
            )
            self.assertEqual(
                [
                    item["content"]
                    for item in ParticipantStore(self.db).history(
                        self.subject, second.interaction_id
                    )
                ],
                [proposal.body],
            )
            with self.db.connection() as connection:
                links = connection.execute(
                    "SELECT entity_id,source_id,conversation_key FROM participant_message_links "
                    "WHERE interaction_id IN (?,?) ORDER BY interaction_id",
                    (first_receipt["interaction_id"], second_receipt["interaction_id"]),
                ).fetchall()
            self.assertEqual(links[0]["entity_id"], links[1]["entity_id"])
            self.assertEqual(links[0]["source_id"], links[1]["source_id"])
            self.assertNotEqual(links[0]["conversation_key"], links[1]["conversation_key"])
            await dispatcher.aclose()

    async def test_matching_reply_does_not_resend_or_settle_an_unknown_delivery(self) -> None:
        from noyra.interaction.transport import DeliveryOutcomeUnknown

        transport, receipt = await self._direct_contact()

        async def uncertain(*args: Any) -> str:
            raise DeliveryOutcomeUnknown(
                "synthetic lost receipt", provider_message_id="unknown-ref"
            )

        dispatcher = DeliveryDispatcher(self.db, self.transports)
        try:
            with patch.object(dispatcher, "_send", side_effect=uncertain) as sender:
                delivery = (await dispatcher.deliver_pending(self.subject))[0]
                self.assertEqual(delivery.status, "unknown")
                received = InboundStore(self.db).ingest(
                    self._direct_reply(transport, "unknown-ref", 1)
                )
                self.assertEqual(
                    InteractionStore(self.db).get(received.interaction_id).related_interaction_id,
                    receipt["interaction_id"],
                )
                self.assertEqual(await dispatcher.deliver_pending(self.subject), [])
                self.assertEqual(dispatcher.get_delivery(delivery.delivery_id).status, "unknown")
                self.assertEqual(sender.call_count, 1)
        finally:
            await dispatcher.aclose()

    async def test_direct_reply_rechecks_source_revocation_before_private_linking(self) -> None:
        transport, _ = await self._direct_contact()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message_id": "revocation-root"})
            )
        ) as client:
            dispatcher = DeliveryDispatcher(self.db, self.transports, client=client)
            await dispatcher.deliver_pending(self.subject)
            await dispatcher.aclose()
        inbound = InboundStore(self.db)
        original_upsert = inbound._upsert_thread

        def revoke_after_initial_admission(
            connection: Any, subject: str, envelope: InboundEnvelope, now: str
        ) -> None:
            original_upsert(connection, subject, envelope, now)
            connection.execute(
                "UPDATE interaction_bindings SET status='revoked' WHERE subject_id=? "
                "AND transport_id=? AND external_sender_id=?",
                (subject, transport.transport_id, "helper@example.com"),
            )

        with (
            patch.object(inbound, "_upsert_thread", side_effect=revoke_after_initial_admission),
            self.assertRaisesRegex(PermissionError, "no longer available"),
        ):
            inbound.ingest(self._direct_reply(transport, "revocation-root", 1))
        with self.db.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM participant_message_links").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM interactions").fetchone()[0], 1
            )

    async def test_operator_revision_preserves_original_subject_author_on_response_and_read(
        self,
    ) -> None:
        proposal = self.article()
        action = self.prepare(proposal)
        receipt = self.store.execute(action, proposal, review_call_id=await self.review(action))
        archive = PublicArchive(self.db)
        edited = archive.publish(
            self.subject,
            {
                "publication_id": receipt["publication_id"],
                "kind": "update",
                "title": proposal.title,
                "summary": proposal.summary,
                "body": "An operator revision.",
                "details": {},
                "expected_revision": 1,
                "confirm_public": True,
            },
            actor="operator",
        )
        self.assertEqual(edited["provenance"], "subject")
        reread = archive.admin_item(self.subject, receipt["publication_id"])
        assert reread is not None
        self.assertEqual(edited["provenance"], reread["provenance"])

    async def test_runtime_lease_invalidation_prevents_local_publication(self) -> None:
        from noyra.core.admission import OperationInvalidated, bind_lease

        proposal = self.article()
        action = self.prepare(proposal)
        review = await self.review(action)
        lease = self.kernel.admission.begin("expression-test")

        def invalidate() -> None:
            self.kernel.admission.invalidate()

        try:
            with bind_lease(lease), self.assertRaises(OperationInvalidated):
                self.store.execute(
                    action,
                    proposal,
                    review_call_id=review,
                    before_commit=invalidate,
                )
        finally:
            self.kernel.admission.finish(lease)
        self.assertEqual(PublicArchive(self.db).admin(self.subject), [])

    async def test_subject_publication_and_self_review_survive_reopen_and_private_export(
        self,
    ) -> None:
        from io import BytesIO
        from zipfile import ZipFile

        from noyra.core import Database, RuntimeLogExporter

        proposal = self.article()
        action = self.prepare(proposal)
        self.store.execute(action, proposal, review_call_id=await self.review(action))
        reopened = Database(self.db.path, initialize=False)
        self.assertEqual(ExpressionStore(reopened, self.subject).verify_integrity(), 1)
        artifact = RuntimeLogExporter(reopened).export(self.subject, actor="operator")
        with ZipFile(BytesIO(artifact.content)) as archive:
            publications = [
                json.loads(line)
                for line in archive.read("tables/public_publications.jsonl").splitlines()
            ]
            revisions = [
                json.loads(line)
                for line in archive.read("tables/public_publication_revisions.jsonl").splitlines()
            ]
            actions = [
                json.loads(line) for line in archive.read("tables/actions.jsonl").splitlines()
            ]
        self.assertEqual(len(publications), 1)
        self.assertEqual(revisions[0]["actor"], "subject")
        self.assertEqual(actions[0]["action_id"], action)
