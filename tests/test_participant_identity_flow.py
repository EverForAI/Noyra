from __future__ import annotations

import json
from pathlib import Path

from noyra.cognition import CognitionSettings, InteractionCognition
from noyra.interaction import InboundEnvelope, InteractionStore
from noyra.interaction.inbound import InboundAccepted
from noyra.interaction.participants import ParticipantStore
from noyra.mind import RelationshipStore
from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger, ModelPricing
from test_inbound import _setup


def test_group_members_keep_separate_identity_and_delivery_route(tmp_path: Path) -> None:
    kernel, transport, inbound = _setup(tmp_path)
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        inbound.bind(
            kernel.subject_id,
            transport.transport_id,
            external_account_id="account",
            external_sender_id="other",
        )

        def receive(sender: str, event: str) -> InboundAccepted:
            return inbound.ingest(
                InboundEnvelope(
                    channel="webhook",
                    transport_id=transport.transport_id,
                    provider_event_id=event,
                    external_account_id="account",
                    external_sender_id=sender,
                    conversation_id="same-group",
                    content="hello",
                    sender_label="Same name",
                )
            )

        first = receive("creator", "first")
        second = receive("other", "second")
        again = receive("creator", "third")
        participants = ParticipantStore(kernel.database)
        a = participants.for_message(kernel.subject_id, first.interaction_id)
        b = participants.for_message(kernel.subject_id, second.interaction_id)
        assert a and b and a.entity_id != b.entity_id
        assert participants.for_message(kernel.subject_id, again.interaction_id) == a
        store = InteractionStore(kernel.database)
        record = store.get(first.interaction_id)
        assert record.counterparty == "same-group"
        gateway = ModelGateway(
            FakeProvider([]),
            ModelLedger(kernel.database),
            model="test-model",
            limits=BudgetLimits(10, 10000, 10000, 100000),
            pricing=ModelPricing(),
        )
        cognition = InteractionCognition(
            kernel.database, kernel.subject_id, gateway, CognitionSettings()
        )
        cognition._revise_relationship(record, "accepted", 0)
        cognition._revise_relationship(store.get(second.interaction_id), "accepted", 0)
        assert {
            r.entity_key for r in RelationshipStore(kernel.database).list(kernel.subject_id)
        } == {a.entity_id, b.entity_id}
        meta = (
            cognition._messages(record)[1]
            .content.split("INTERACTION_METADATA_JSON\n", 1)[1]
            .split("\nBEGIN_", 1)[0]
        )
        assert json.loads(meta)["participant"]["entity_id"] == a.entity_id
        reply = store.send(
            kernel.subject_id,
            record.channel,
            record.counterparty,
            "response",
            related_interaction_id=record.interaction_id,
        )
        reply_owner = participants.for_message(kernel.subject_id, reply.interaction_id)
        assert reply_owner and reply_owner.entity_id == a.entity_id
        assert reply.counterparty == "same-group"
        assert participants.history(kernel.subject_id, second.interaction_id) == []
    finally:
        kernel.close()
