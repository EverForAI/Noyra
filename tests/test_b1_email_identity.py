from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any
from unittest.mock import patch

import pytest

import test_c0_expression_store as expression_base
from noyra.interaction import (
    DeliveryDispatcher,
    EmailInboundAdapter,
    InboundEnvelope,
    InboundStore,
    InteractionStore,
)
from noyra.interaction.participants import ParticipantStore
from noyra.interaction.types import InteractionDecision


@pytest.fixture
def mail_case() -> Iterator[expression_base.ExpressionStoreTestCase]:
    case = expression_base.ExpressionStoreTestCase()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def signed(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = "synthetic-mail-nonce"
    digest = hmac.new(
        b"synthetic-gateway-secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"X-Noyra-Timestamp": timestamp, "X-Noyra-Nonce": nonce, "X-Noyra-Signature": digest}


def raw_mail(
    *,
    sender: str = "helper@example.com",
    message_id: str = "<same@example.com>",
    reference: str | None = None,
    text: str = "A public suggestion.",
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "noyra@example.com"
    message["Message-ID"] = message_id
    message["Authentication-Results"] = "untrusted.example; dkim=fail; spf=fail; dmarc=fail"
    if reference:
        message["In-Reply-To"] = reference
    message.set_content(text)
    return message.as_bytes()


def parse(body: bytes, transport: str) -> InboundEnvelope:
    return EmailInboundAdapter().parse(
        body, signed(body), transport_id=transport, secret="synthetic-gateway-secret"
    )


@pytest.mark.asyncio
async def test_gateway_signed_raw_from_cannot_inherit_mailbox_private_history(
    mail_case: expression_base.ExpressionStoreTestCase,
) -> None:
    case = mail_case
    transport, _ = await case._direct_contact("email")

    def send(
        endpoint: str,
        credentials: dict[str, Any],
        recipient: str,
        content: str,
        key: str,
        authorize_send: Any = None,
        **kwargs: Any,
    ) -> str:
        if authorize_send is not None:
            authorize_send()
        return DeliveryDispatcher._smtp_message_id(key)

    dispatcher = DeliveryDispatcher(case.db, case.transports)
    try:
        with patch.object(DeliveryDispatcher, "_send_email", staticmethod(send)):
            first = (await dispatcher.deliver_pending(case.subject))[0]
            accepted = InboundStore(case.db).ingest(
                case._direct_reply(transport, str(first.provider_message_id), 1)
            )
            InteractionStore(case.db).decide(
                accepted.interaction_id,
                InteractionDecision(disposition="accepted", rationale="Synthetic dialogue"),
            )
            InteractionStore(case.db).send(
                case.subject,
                "email",
                "helper@example.com",
                "SYNTHETIC_PRIVATE_HISTORY_ONLY",
                related_interaction_id=accepted.interaction_id,
                idempotency_key="synthetic-private-followup",
            )
            followup = (await dispatcher.deliver_pending(case.subject))[0]
            body = raw_mail(reference=str(followup.provider_message_id))
            forged = InboundStore(case.db).ingest(parse(body, transport.transport_id))
        assert ParticipantStore(case.db).history(case.subject, forged.interaction_id) == []
        assert InteractionStore(case.db).get(forged.interaction_id).related_interaction_id is None
    finally:
        await dispatcher.aclose()


def test_same_message_id_from_other_sender_does_not_occupy_event_identity() -> None:
    first = parse(raw_mail(sender="alice@example.com"), "synthetic-mail")
    second = parse(raw_mail(sender="bob@example.com"), "synthetic-mail")
    assert first.provider_event_id != second.provider_event_id


def gateway_mail(
    raw: bytes,
    *,
    event: str = "gateway-event-one",
    sender: str = "helper@example.com",
    method: str | None = "provider_mailbox",
    account: str = "noyra@example.com",
) -> bytes:
    payload: dict[str, Any] = {
        "format": "noyra-email-ingress/v1",
        "event_id": event,
        "account": account,
        "raw_message_b64": base64.b64encode(raw).decode("ascii"),
    }
    if method is not None:
        payload["sender_assertion"] = {"mailbox": sender, "method": method}
    return json.dumps(payload, separators=(",", ":")).encode()
