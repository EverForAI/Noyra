"""Mail ingress evidence: gateway delivery authentication is not mailbox control."""

from __future__ import annotations

import base64
import binascii
import json
import re
from email.utils import getaddresses
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash

MAX_MAIL_ID = 512
MAX_MAIL_REFERENCES = 32
MAILBOX_AUTHENTICATION = "credential"
MailVerification = Literal[
    "unverified", "mailbox_challenge", "authenticated_submission", "provider_mailbox"
]


def mailbox(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_MAIL_ID:
        raise ValueError(f"email {field} address is invalid")
    addresses = [address.strip() for _, address in getaddresses([value]) if address.strip()]
    if len(addresses) != 1:
        raise ValueError(f"email {field} address is invalid")
    address = addresses[0]
    local, separator, domain = address.rpartition("@")
    if (
        not separator
        or not local
        or not domain
        or len(address) > MAX_MAIL_ID
        or any(ord(char) < 32 or ord(char) == 127 for char in address)
    ):
        raise ValueError(f"email {field} address is invalid")
    return f"{local}@{domain.casefold()}"


def single_header(message: Any, name: str) -> str | None:
    values = message.get_all(name, [])
    if len(values) > 1:
        raise ValueError(f"email duplicate {name} header")
    return str(values[0]).strip() if values else None


def message_ids(value: str | None, name: str, *, multiple: bool = False) -> list[str]:
    if value is None:
        return []
    # Accept a bounded sequence of bracketed opaque IDs, not a substring of
    # an ambiguous header. Folding is allowed only between complete IDs.
    normalized = " ".join(value.split())
    limit = MAX_MAIL_ID * MAX_MAIL_REFERENCES if multiple else MAX_MAIL_ID
    if not normalized or len(value) > limit:
        raise ValueError(f"email {name} is invalid")
    ids = re.findall(r"<[^<>\s\x00-\x1f\x7f]+>", normalized)
    if (
        " ".join(ids) != normalized
        or not ids
        or len(ids) > (MAX_MAIL_REFERENCES if multiple else 1)
        or any(len(item) > MAX_MAIL_ID for item in ids)
    ):
        raise ValueError(f"email {name} is invalid")
    return ids


class MailboxAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    mailbox: str = Field(min_length=3, max_length=MAX_MAIL_ID)
    method: Literal["mailbox_challenge", "authenticated_submission", "provider_mailbox"]


class EmailGatewayPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format: Literal["noyra-email-ingress/v1"]
    event_id: str = Field(min_length=1, max_length=MAX_MAIL_ID, pattern=r"^\S+$")
    account: str = Field(min_length=3, max_length=MAX_MAIL_ID)
    raw_message_b64: str = Field(min_length=1, max_length=1_048_576)
    sender_assertion: MailboxAssertion | None = None


class EmailIdentity(BaseModel):
    """Created only after authenticating ingress; never decoded from MIME headers."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    message_id: str | None = Field(default=None, max_length=MAX_MAIL_ID)
    gateway_event_id: str | None = Field(default=None, max_length=MAX_MAIL_ID)
    verification: MailVerification = "unverified"
    raw_message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_provenance(self) -> EmailIdentity:
        if self.message_id is not None:
            message_ids(self.message_id, "Message-ID")
        if self.gateway_event_id is not None and (
            not self.gateway_event_id
            or any(ord(char) < 33 or ord(char) == 127 for char in self.gateway_event_id)
        ):
            raise ValueError("email gateway event identity is invalid")
        if self.verification != "unverified" and self.gateway_event_id is None:
            raise ValueError("verified email requires gateway event provenance")
        return self

    @property
    def verified(self) -> bool:
        return self.verification != "unverified"

    def event_key(self, sender: str) -> str:
        # A raw sender-controlled Message-ID can neither preempt a different
        # sender nor a gateway-verified event. Reusing an ID with different
        # content within one source remains a conflict, not silent deduplication.
        return "mail-v1:" + content_hash(
            [
                "gateway" if self.gateway_event_id is not None else "raw",
                self.verified,
                sender,
                self.gateway_event_id or self.message_id or self.raw_message_hash,
            ]
        )

    def namespace(self, transport: str, account: str, sender: str) -> str:
        return content_hash(
            [
                "email-source/v1",
                transport,
                account,
                "mailbox" if self.verified else self.event_key(sender),
            ]
        )


def decode_gateway(body: bytes) -> tuple[bytes, EmailGatewayPayload | None]:
    if not body.lstrip().startswith(b"{"):
        return body, None

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("email gateway JSON has duplicate fields")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("email gateway JSON has a non-finite value")

    try:
        decoded = json.loads(
            body.decode("utf-8"), object_pairs_hook=unique_fields, parse_constant=reject_constant
        )
    except RecursionError as error:
        raise ValueError("email gateway JSON nesting exceeds limit") from error
    payload = EmailGatewayPayload.model_validate(decoded)
    try:
        raw = base64.b64decode(payload.raw_message_b64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("email gateway raw message encoding is invalid") from error
    if not raw or len(raw) > 1_048_576:
        raise ValueError("email gateway raw message exceeds limit")
    return raw, payload


def evidence_state(event: Any, identity: EmailIdentity, namespace: str) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "subject_id": event["subject_id"],
        "transport_id": event["transport_id"],
        "external_account_id": event["external_account_id"],
        "external_sender_id": event["external_sender_id"],
        "provider_event_id": event["provider_event_id"],
        "conversation_id": event["conversation_id"],
        "external_thread_id": event["external_thread_id"],
        "content_hash": event["content_hash"],
        "identity": identity.model_dump(),
        "source_namespace": namespace,
    }


def persist_evidence(connection: Any, event: Any, identity: EmailIdentity) -> None:
    namespace = identity.namespace(
        event["transport_id"], event["external_account_id"], event["external_sender_id"]
    )
    row = connection.execute(
        "SELECT * FROM email_ingress_evidence WHERE event_id=?", (event["event_id"],)
    ).fetchone()
    state_hash = content_hash(evidence_state(event, identity, namespace))
    if row is not None:
        existing = read_evidence(connection, event)
        if existing != identity or row["state_hash"] != state_hash:
            raise ValueError("inbound email event id identifies different evidence")
        return
    connection.execute(
        "INSERT INTO email_ingress_evidence(event_id,subject_id,message_id,gateway_event_id,"
        "verification,raw_message_hash,source_namespace,state_hash) VALUES(?,?,?,?,?,?,?,?)",
        (
            event["event_id"],
            event["subject_id"],
            identity.message_id,
            identity.gateway_event_id,
            identity.verification,
            identity.raw_message_hash,
            namespace,
            state_hash,
        ),
    )


def read_evidence(connection: Any, event: Any) -> EmailIdentity | None:
    row = connection.execute(
        "SELECT * FROM email_ingress_evidence WHERE event_id=?", (event["event_id"],)
    ).fetchone()
    if row is None:
        if int(event["email_identity_version"]) != 0:
            raise IntegrityError("email identity evidence is missing")
        return None  # Legacy rows never gain proof by migration.
    try:
        identity = EmailIdentity(
            message_id=row["message_id"],
            gateway_event_id=row["gateway_event_id"],
            verification=row["verification"],
            raw_message_hash=row["raw_message_hash"],
        )
        namespace = identity.namespace(
            event["transport_id"], event["external_account_id"], event["external_sender_id"]
        )
        if (
            event["channel"] != "email"
            or int(event["reply_context_version"]) != 1
            or int(event["email_identity_version"]) != 1
            or row["subject_id"] != event["subject_id"]
            or row["source_namespace"] != namespace
            or event["provider_event_id"] != identity.event_key(event["external_sender_id"])
            or row["state_hash"] != content_hash(evidence_state(event, identity, namespace))
        ):
            raise ValueError("email evidence binding mismatch")
    except (TypeError, ValueError) as error:
        raise IntegrityError("email identity evidence is invalid") from error
    return identity
