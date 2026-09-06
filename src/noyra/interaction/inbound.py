"""Native inbound message boundaries for external communication channels.

Adapters only decode and authenticate provider envelopes.  They never execute
commands or forward arbitrary provider payloads.  ``InboundStore`` performs
durable replay protection, account binding, and maps accepted text into the
existing invitation-based ``InteractionStore``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .store import InteractionStore

InboundChannel = Literal["telegram", "qq", "feishu", "wechat", "email", "webhook"]
InboundRole = Literal["creator", "participant"]
InboundReplySelector = Literal["qq:user", "qq:group", "qq:channel", "qq:dm", "feishu:chat_id"]

MAX_INBOUND_BODY_BYTES = 1_048_576
MAX_INBOUND_CONTENT_CHARS = 100_000
MAX_INBOUND_ID_CHARS = 512
REPLAY_WINDOW_SECONDS = 300
MAX_WECHAT_XML_NODES = 1024
MAX_WECHAT_XML_DEPTH = 64
MAX_EMAIL_MIME_PARTS = 256
MAX_EMAIL_MIME_DEPTH = 32


class InboundAuthenticationError(ValueError):
    """Provider authentication or replay validation failed."""


class InboundEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel: InboundChannel
    transport_id: str = Field(min_length=1, max_length=128)
    provider_event_id: str = Field(min_length=1, max_length=MAX_INBOUND_ID_CHARS)
    external_account_id: str = Field(min_length=1, max_length=512)
    external_sender_id: str = Field(min_length=1, max_length=512)
    conversation_id: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=MAX_INBOUND_CONTENT_CHARS)
    sender_label: str = Field(default="", max_length=512)
    thread_id: str | None = Field(default=None, max_length=512)
    reply_selector: InboundReplySelector | None = None
    received_at: str = Field(default_factory=utc_now)
    sender_role: InboundRole = "participant"

    @field_validator(
        "transport_id",
        "provider_event_id",
        "external_account_id",
        "external_sender_id",
        "conversation_id",
        "content",
        "sender_label",
        "thread_id",
    )
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value and cls.model_fields.get("sender_label"):
            # Empty labels are allowed; every identity and message field is not.
            return value
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> InboundEnvelope:
        if not self.content:
            raise ValueError("inbound content cannot be blank")
        if self.channel == "email" and not self.conversation_id:
            raise ValueError("email conversation id is required")
        if self.channel in {"qq", "feishu"} and self.reply_selector is None:
            raise ValueError("native inbound reply selector is required")
        if self.reply_selector is not None and not self.reply_selector.startswith(
            f"{self.channel}:"
        ):
            raise ValueError("inbound reply selector does not match channel")
        return self


@dataclass(frozen=True)
class InboundAccepted:
    event_id: str
    interaction_id: str
    duplicate: bool
    scheduling_priority: int


@dataclass(frozen=True)
class InboundChallenge:
    challenge: str
    response: dict[str, str] | None = None


@dataclass(frozen=True)
class InboundIgnored:
    """An authenticated provider event that carries no chat message."""

    reason: str


class InboundAdapter(Protocol):
    channel: InboundChannel

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope | InboundChallenge | InboundIgnored: ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _bounded_body(body: bytes) -> bytes:
    if not isinstance(body, bytes) or len(body) > MAX_INBOUND_BODY_BYTES:
        raise ValueError("inbound body exceeds limit")
    return body


def _json(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(_bounded_body(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("inbound JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise ValueError("inbound JSON object is required")
    return parsed


def _text(value: Any, field: str, *, required: bool = True, limit: int = 512) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        if required:
            raise ValueError(f"inbound {field} is invalid")
        return ""
    result = str(value).strip()
    if required and not result:
        raise ValueError(f"inbound {field} is blank")
    if len(result) > limit:
        raise ValueError(f"inbound {field} exceeds limit")
    return result


def _verify_hmac(body: bytes, headers: Mapping[str, str], secret: str, *, header: str) -> None:
    if not secret:
        raise InboundAuthenticationError("inbound signature secret is not configured")
    signature = _header(headers, header)
    if not signature:
        raise InboundAuthenticationError("inbound signature is missing")
    timestamp = _header(headers, "X-Noyra-Timestamp")
    nonce = _header(headers, "X-Noyra-Nonce") or ""
    if timestamp is None:
        raise InboundAuthenticationError("inbound signature timestamp is missing")
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise InboundAuthenticationError("inbound signature timestamp is invalid") from error
    if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
        raise InboundAuthenticationError("inbound signature is stale")
    message = f"{timestamp_value}.{nonce}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    candidate = signature.removeprefix("sha256=")
    valid = hmac.compare_digest(candidate.casefold(), digest.hex())
    if not valid:
        try:
            valid = hmac.compare_digest(base64.b64decode(candidate, validate=True), digest)
        except (binascii.Error, ValueError):
            valid = False
    if not valid:
        raise InboundAuthenticationError("inbound signature is invalid")


def _verify_feishu_signature(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    required: bool,
) -> None:
    """Validate Feishu's optional Encrypt Key signature envelope.

    Feishu's official event SDK skips signature verification when no Encrypt
    Key is configured.  When an Encrypt Key is configured, the three Lark
    headers are mandatory and the signature covers the original request body
    (the encrypted wrapper, when present), not the decrypted JSON.
    """
    if not secret:
        if required:
            raise InboundAuthenticationError("feishu signature secret is not configured")
        return
    timestamp = _header(headers, "X-Lark-Request-Timestamp")
    nonce = _header(headers, "X-Lark-Request-Nonce")
    signature = _header(headers, "X-Lark-Signature")
    if not timestamp or not nonce or not signature:
        if required or any((timestamp, nonce, signature)):
            raise InboundAuthenticationError("feishu signature is missing")
        return
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise InboundAuthenticationError("feishu signature timestamp is invalid") from error
    if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
        raise InboundAuthenticationError("feishu signature is stale")
    expected = hashlib.sha256(f"{timestamp}{nonce}{secret}".encode() + body).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise InboundAuthenticationError("feishu signature is invalid")


def _verify_wechat_signature(headers: Mapping[str, str], secret: str) -> None:
    if not secret:
        raise InboundAuthenticationError("wechat signature secret is not configured")
    signature = _header(headers, "X-Wechat-Signature")
    timestamp = _header(headers, "X-Wechat-Timestamp")
    nonce = _header(headers, "X-Wechat-Nonce")
    if not signature or not timestamp or not nonce:
        raise InboundAuthenticationError("wechat signature is missing")
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise InboundAuthenticationError("wechat signature timestamp is invalid") from error
    if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
        raise InboundAuthenticationError("wechat signature is stale")
    expected = hashlib.sha1("".join(sorted((secret, timestamp, nonce))).encode()).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise InboundAuthenticationError("wechat signature is invalid")


def _qq_private_key(secret: str) -> Any:
    """Build QQ's official Ed25519 key from the AppSecret seed."""
    if not secret:
        raise InboundAuthenticationError("qq app secret is not configured")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed_source = secret.encode("utf-8")
        seed = (seed_source * ((32 + len(seed_source) - 1) // len(seed_source)))[:32]
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (ImportError, ValueError, TypeError) as error:
        raise InboundAuthenticationError("qq app secret is invalid") from error


def _verify_qq_signature(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    legacy_secret: str | None = None,
) -> None:
    """Verify QQ Bot callbacks using its official Ed25519 envelope.

    QQ derives the webhook Ed25519 key pair from AppSecret.  A legacy Noyra
    HMAC header is accepted only with an explicit legacy secret; a public-key
    field can never silently become an HMAC key.
    """
    signature = _header(headers, "X-Signature-Ed25519")
    timestamp = _header(headers, "X-Signature-Timestamp")
    if not signature and not timestamp:
        if not legacy_secret:
            raise InboundAuthenticationError("qq legacy webhook secret is not configured")
        _verify_hmac(body, headers, legacy_secret, header="X-Noyra-Signature")
        return
    if not signature or not timestamp:
        raise InboundAuthenticationError("qq signature is missing")
    try:
        if len(timestamp) > 32 or abs(time.time() - int(timestamp)) > REPLAY_WINDOW_SECONDS:
            raise InboundAuthenticationError("qq signature is stale")
    except ValueError as error:
        raise InboundAuthenticationError("qq signature timestamp is invalid") from error
    try:
        signature_bytes = bytes.fromhex(signature)
    except ValueError as error:
        raise InboundAuthenticationError("qq signature is invalid") from error
    try:
        _qq_private_key(secret).public_key().verify(
            signature_bytes, timestamp.encode("ascii") + body
        )
    except (ImportError, ValueError, TypeError) as error:
        raise InboundAuthenticationError("qq app secret is invalid") from error
    except Exception as error:
        # Ed25519InvalidSignature is intentionally not imported at module
        # import time; cryptography remains an optional inbound dependency.
        raise InboundAuthenticationError("qq signature is invalid") from error


def _common_envelope(
    *,
    channel: InboundChannel,
    transport_id: str,
    event_id: Any,
    account: Any,
    sender: Any,
    conversation: Any,
    content: Any,
    label: Any = "",
    thread: Any = None,
    reply_selector: InboundReplySelector | None = None,
    creator: bool = False,
) -> InboundEnvelope:
    return InboundEnvelope(
        channel=channel,
        transport_id=_text(transport_id, "transport_id"),
        provider_event_id=_text(event_id, "provider_event_id", limit=MAX_INBOUND_ID_CHARS),
        external_account_id=_text(account, "external_account_id"),
        external_sender_id=_text(sender, "external_sender_id"),
        conversation_id=_text(conversation, "conversation_id"),
        content=_text(content, "content", limit=MAX_INBOUND_CONTENT_CHARS),
        sender_label=_text(label, "sender_label", required=False),
        thread_id=_text(thread, "thread_id", required=False) or None,
        reply_selector=reply_selector,
        sender_role="creator" if creator else "participant",
    )


class TelegramInboundAdapter:
    channel: InboundChannel = "telegram"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope:
        expected = _header(headers, "X-Telegram-Bot-Api-Secret-Token")
        if not secret or not expected or not hmac.compare_digest(expected, secret):
            raise InboundAuthenticationError("telegram webhook secret is invalid")
        payload = _json(body)
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            raise ValueError("telegram message event is missing")
        chat = message.get("chat")
        sender = message.get("from") or {}
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            raise ValueError("telegram message identity is invalid")
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            event_id=payload.get("update_id"),
            account=chat.get("id"),
            sender=sender.get("id"),
            conversation=chat.get("id"),
            content=message.get("text") or message.get("caption"),
            label=sender.get("username") or sender.get("first_name"),
            thread=message.get("message_thread_id"),
        )


class FeishuInboundAdapter:
    channel: InboundChannel = "feishu"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope | InboundChallenge | InboundIgnored:
        original_body = _bounded_body(body)
        payload = _json(original_body)
        encrypted = payload.get("encrypt")
        if encrypted is not None:
            if not isinstance(encrypted, str) or not encrypted.strip():
                raise ValueError("feishu encrypted event is invalid")
            if not encryption_key:
                raise InboundAuthenticationError("feishu encrypted event is not configured")
            _verify_feishu_signature(original_body, headers, encryption_key, required=True)
            payload = _json(self._decrypt_payload(encrypted, encryption_key))
        else:
            # Plaintext callbacks are authenticated by the configured
            # verification token.  If an Encrypt Key is configured Feishu's
            # SDK requires the corresponding signature even for plaintext.
            _verify_feishu_signature(
                original_body,
                headers,
                encryption_key or "",
                required=bool(encryption_key),
            )
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        token = payload.get("token")
        if isinstance(header, dict):
            token = header.get("token", token)
        if token is None:
            raise InboundAuthenticationError("feishu verification token is missing")
        token_text = _text(token, "feishu verification token")
        if not hmac.compare_digest(token_text, secret):
            raise InboundAuthenticationError("feishu verification token is invalid")
        if payload.get("type") == "url_verification":
            return InboundChallenge(_text(payload.get("challenge"), "challenge"))
        event_type = ""
        if isinstance(header, dict):
            event_type = str(header.get("event_type") or "").strip()
        if not event_type:
            event_type = str(payload.get("event_type") or "").strip()
        if event_type and event_type not in {"im.message.receive_v1", "message.receive_v1"}:
            return InboundIgnored("feishu_non_message_event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            raise ValueError("feishu event envelope is invalid")
        sender_container = event.get("sender") or {}
        if not isinstance(sender_container, dict):
            raise ValueError("feishu sender envelope is invalid")
        sender = sender_container.get("sender_id") or {}
        message = event.get("message") or {}
        if not isinstance(sender, dict) or not isinstance(message, dict):
            if event_type:
                return InboundIgnored("feishu_non_message_event")
            raise ValueError("feishu message envelope is invalid")
        message_type = str(message.get("message_type") or "").casefold()
        if message_type and message_type != "text":
            return InboundIgnored("feishu_non_text_message")
        content = message.get("content")
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError) as error:
                if event_type:
                    return InboundIgnored("feishu_non_text_message")
                raise ValueError("feishu message content is invalid") from error
            if not isinstance(decoded, dict):
                if event_type:
                    return InboundIgnored("feishu_non_text_message")
                raise ValueError("feishu message content is invalid")
            content = decoded.get("text")
        if not isinstance(content, str) or not content.strip():
            if event_type:
                return InboundIgnored("feishu_non_text_message")
            raise ValueError("feishu message content is invalid")
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            # V2 callbacks carry identity in ``header``; legacy V1 callbacks
            # use the top-level uuid/app fields.  Both are official event
            # envelopes and must deduplicate to the provider event ID.
            event_id=(
                header.get("event_id")
                or payload.get("uuid")
                or event.get("event_id")
                or message.get("message_id")
            ),
            account=(
                header.get("app_id") or event.get("app_id") or payload.get("app_id") or "feishu"
            ),
            sender=(sender.get("open_id") or sender.get("user_id")),
            conversation=message.get("chat_id"),
            content=content,
            label=sender.get("name"),
            thread=message.get("thread_id"),
            reply_selector="feishu:chat_id",
        )

    @staticmethod
    def _decrypt_payload(encrypted: str, encryption_key: str) -> bytes:
        """Decrypt the official Feishu AES-CBC event wrapper."""
        try:
            encrypted_bytes = base64.b64decode(encrypted, validate=True)
            if len(encrypted_bytes) <= 16 or len(encrypted_bytes[16:]) % 16:
                raise ValueError("feishu ciphertext is invalid")
            key = hashlib.sha256(encryption_key.encode("utf-8")).digest()
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            decryptor = Cipher(
                # The official Lark SDK prefixes the encrypted payload with
                # its random 16-byte CBC IV.
                algorithms.AES(key),
                modes.CBC(encrypted_bytes[:16]),
            ).decryptor()
            padded = decryptor.update(encrypted_bytes[16:]) + decryptor.finalize()
            padding = padded[-1]
            if not 1 <= padding <= 16 or padded[-padding:] != bytes([padding]) * padding:
                raise ValueError("feishu ciphertext padding is invalid")
            plaintext = padded[:-padding]
            if not plaintext or len(plaintext) > MAX_INBOUND_BODY_BYTES:
                raise ValueError("feishu plaintext is invalid")
            return plaintext
        except (ValueError, binascii.Error, ImportError) as error:
            raise ValueError("feishu encrypted event cannot be decrypted") from error


class QQInboundAdapter:
    channel: InboundChannel = "qq"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope | InboundChallenge | InboundIgnored:
        payload = _json(body)
        op = payload.get("op")
        data = payload.get("d")
        if op == 13:
            # QQ's callback-address validation request is intentionally
            # unsigned.  Possession of AppSecret is proven by the Ed25519
            # response signature over event_ts + plain_token.
            if not isinstance(data, dict):
                raise ValueError("qq validation payload is invalid")
            plain_token = _text(data.get("plain_token"), "qq plain_token")
            event_ts = _text(data.get("event_ts"), "qq event_ts")
            signature = _qq_private_key(secret).sign((event_ts + plain_token).encode("utf-8"))
            return InboundChallenge(
                plain_token,
                {"plain_token": plain_token, "signature": signature.hex()},
            )
        _verify_qq_signature(body, headers, secret, legacy_secret=signing_key)
        event_type = str(payload.get("t") or "").strip()
        message_types = {
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "GROUP_MESSAGE_CREATE",
            "AT_MESSAGE_CREATE",
            "MESSAGE_CREATE",
            "GUILD_MESSAGE_CREATE",
            "GUILD_AT_MESSAGE_CREATE",
            "DIRECT_MESSAGE_CREATE",
        }
        if event_type and event_type not in message_types:
            return InboundIgnored("qq_non_message_event")
        event_payload = data if isinstance(data, dict) and isinstance(op, int) else payload
        author = event_payload.get("author") or {}
        if not isinstance(author, dict):
            if event_type:
                return InboundIgnored("qq_non_message_event")
            raise ValueError("qq author envelope is invalid")
        content = event_payload.get("content")
        if event_type and (not isinstance(content, str) or not content.strip()):
            return InboundIgnored("qq_non_text_event")
        if event_type == "C2C_MESSAGE_CREATE":
            reply_selector: InboundReplySelector = "qq:user"
            conversation = (
                author.get("user_openid") or author.get("id") or event_payload.get("openid")
            )
        elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
            reply_selector = "qq:group"
            conversation = event_payload.get("group_openid") or event_payload.get("group_id")
        elif event_type == "DIRECT_MESSAGE_CREATE":
            reply_selector = "qq:dm"
            conversation = event_payload.get("guild_id")
        elif event_type in {
            "AT_MESSAGE_CREATE",
            "MESSAGE_CREATE",
            "GUILD_MESSAGE_CREATE",
            "GUILD_AT_MESSAGE_CREATE",
        }:
            reply_selector = "qq:channel"
            conversation = event_payload.get("channel_id")
        elif event_payload.get("group_openid") or event_payload.get("group_id"):
            # Compatibility for the pre-envelope HMAC callback shape.
            reply_selector = "qq:group"
            conversation = event_payload.get("group_openid") or event_payload.get("group_id")
        elif event_payload.get("channel_id"):
            reply_selector = "qq:channel"
            conversation = event_payload.get("channel_id")
        elif event_payload.get("guild_id"):
            reply_selector = "qq:dm"
            conversation = event_payload.get("guild_id")
        else:
            reply_selector = "qq:user"
            conversation = (
                author.get("user_openid") or author.get("id") or event_payload.get("openid")
            )
        reference = event_payload.get("message_reference")
        if reference is not None and not isinstance(reference, dict):
            raise ValueError("qq message reference is invalid")
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            event_id=event_payload.get("id") or event_payload.get("event_id"),
            account=event_payload.get("app_id") or event_payload.get("guild_id") or "qq",
            sender=(author.get("user_openid") or author.get("member_openid") or author.get("id")),
            conversation=conversation,
            content=content,
            label=author.get("username") or author.get("nickname"),
            # Passive replies must bind to the current inbound message.  A
            # message_reference points at an older item quoted by the user and
            # is context, not the provider reply target.
            thread=(event_payload.get("id") or event_payload.get("message_id")),
            reply_selector=reply_selector,
        )


class WeChatInboundAdapter:
    channel: InboundChannel = "wechat"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope | InboundIgnored:
        raw = _bounded_body(body).lstrip()
        if raw.startswith(b"<"):
            try:
                root = self._parse_xml(raw)
                values = {child.tag: (child.text or "") for child in root}
            except (ET.ParseError, ValueError) as error:
                raise ValueError("wechat XML is invalid") from error
            encrypted = values.get("Encrypt", "").strip()
            if encrypted:
                if not encryption_key:
                    raise ValueError("wechat encrypted callback is not configured")
                if not isinstance(account_id, str) or not account_id.strip():
                    raise ValueError("wechat encrypted callback account is not configured")
                signature = _header(headers, "X-Wechat-MsgSignature")
                timestamp = _header(headers, "X-Wechat-Timestamp")
                nonce = _header(headers, "X-Wechat-Nonce")
                if not signature or not timestamp or not nonce:
                    raise InboundAuthenticationError("wechat encrypted signature is missing")
                try:
                    timestamp_value = int(timestamp)
                except ValueError as error:
                    raise InboundAuthenticationError(
                        "wechat signature timestamp is invalid"
                    ) from error
                if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
                    raise InboundAuthenticationError("wechat signature is stale")
                expected = hashlib.sha1(
                    "".join(sorted((secret, timestamp, nonce, encrypted))).encode()
                ).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise InboundAuthenticationError("wechat encrypted signature is invalid")
                raw = self._decrypt_xml(
                    encrypted,
                    encryption_key,
                    expected_account_id=account_id,
                )
                root = self._parse_xml(raw)
                values = {child.tag: (child.text or "") for child in root}
            else:
                _verify_wechat_signature(headers, secret)
            if values.get("MsgType", "").strip().casefold() != "text":
                return InboundIgnored("wechat_non_text_event")
            return _common_envelope(
                channel=self.channel,
                transport_id=transport_id,
                event_id=values.get("MsgId") or content_hash(values),
                account=values.get("ToUserName"),
                sender=values.get("FromUserName"),
                conversation=values.get("FromUserName"),
                content=values.get("Content"),
                label="",
            )
        _verify_wechat_signature(headers, secret)
        payload = _json(body)
        message_type = str(payload.get("msgtype") or payload.get("type") or "").casefold()
        if message_type and message_type != "text":
            return InboundIgnored("wechat_non_text_event")
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            event_id=payload.get("msgid") or payload.get("event_id"),
            account=payload.get("to") or payload.get("account"),
            sender=payload.get("from") or payload.get("sender"),
            conversation=payload.get("conversation_id") or payload.get("from"),
            content=payload.get("content") or payload.get("text"),
            label=payload.get("nickname"),
        )

    @staticmethod
    def _parse_xml(raw: bytes) -> ET.Element:
        """Parse bounded plaintext XML after rejecting entity declarations."""
        if raw.startswith(b"<"):
            try:
                # ElementTree permits internal entity expansion.  WeChat's
                # callback format never needs a DTD, so reject declarations
                # before parsing and bound the resulting tree as defense in
                # depth against entity/structural expansion attacks.
                # CDATA sections are part of the official WeChat XML envelope;
                # reject only declarations that can introduce external entities.
                first_element = re.search(rb"<\s*[A-Za-z_][A-Za-z0-9_.:-]*", raw)
                declaration_region = raw if first_element is None else raw[: first_element.start()]
                if any(
                    marker in declaration_region.upper() for marker in (b"<!DOCTYPE", b"<!ENTITY")
                ):
                    raise ValueError("wechat XML declarations are not allowed")
                root = ET.fromstring(raw)
                nodes = 0
                text_size = 0

                def visit(node: ET.Element, depth: int = 0) -> None:
                    nonlocal nodes, text_size
                    nodes += 1
                    if nodes > MAX_WECHAT_XML_NODES or depth > MAX_WECHAT_XML_DEPTH:
                        raise ValueError("wechat XML structure exceeds limit")
                    text_size += len(node.text or "") + len(node.tail or "")
                    if text_size > MAX_INBOUND_CONTENT_CHARS:
                        raise ValueError("wechat XML text exceeds limit")
                    for child in node:
                        visit(child, depth + 1)

                visit(root)
                return root
            except (ET.ParseError, ValueError) as error:
                raise ValueError("wechat XML is invalid") from error
        raise ValueError("wechat XML body is required")

    @staticmethod
    def _decrypt_xml(
        encrypted: str,
        encoding_key: str,
        *,
        expected_account_id: str,
    ) -> bytes:
        try:
            key_text = encoding_key.strip()
            key_text += "=" * ((4 - len(key_text) % 4) % 4)
            key = base64.b64decode(key_text, validate=True)
            ciphertext = base64.b64decode(encrypted, validate=True)
            if len(key) != 32 or not ciphertext or len(ciphertext) % 16:
                raise ValueError("wechat AES payload is invalid")
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            padding = padded[-1]
            if not 1 <= padding <= 32 or padded[-padding:] != bytes([padding]) * padding:
                raise ValueError("wechat AES padding is invalid")
            plaintext = padded[:-padding]
            if len(plaintext) < 20:
                raise ValueError("wechat AES plaintext is invalid")
            length = int.from_bytes(plaintext[16:20], "big")
            message = plaintext[20 : 20 + length]
            app_id = plaintext[20 + length :].decode("utf-8")
            if len(message) != length or not app_id:
                raise ValueError("wechat AES plaintext is invalid")
        except (ValueError, UnicodeDecodeError, binascii.Error, ImportError) as error:
            raise ValueError("wechat encrypted callback cannot be decrypted") from error
        if not expected_account_id.strip() or not hmac.compare_digest(
            app_id, expected_account_id.strip()
        ):
            raise InboundAuthenticationError("wechat AES account identity is invalid")
        return message


class EmailInboundAdapter:
    channel: InboundChannel = "email"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope:
        _verify_hmac(body, headers, secret, header="X-Noyra-Signature")
        message = BytesParser(policy=policy.default).parsebytes(_bounded_body(body))
        self._validate_mime_structure(message)
        sender = self._mailbox(message.get("From"), "sender")
        recipient = self._mailbox(message.get("To"), "recipient")
        message_id = str(message.get("Message-ID") or content_hash(body)).strip()
        content = message.get_body(preferencelist=("plain", "html"))
        if content is None:
            raise ValueError("email text body is missing")
        text = content.get_content() if hasattr(content, "get_content") else str(content)
        in_reply_to = str(message.get("In-Reply-To") or "").strip()
        references = str(message.get("References") or "").split()
        conversation_id = in_reply_to or (references[0] if references else message_id)
        thread_id = in_reply_to or (references[-1] if references else None)
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            event_id=message_id,
            account=recipient,
            sender=sender,
            conversation=conversation_id,
            content=text,
            label=sender,
            thread=thread_id,
        )

    @staticmethod
    def _mailbox(value: Any, field: str) -> str:
        """Return one canonical mailbox, rejecting ambiguous address headers."""
        raw = _text(value, field, limit=MAX_INBOUND_ID_CHARS)
        addresses = [address.strip() for _, address in getaddresses([raw]) if address.strip()]
        if len(addresses) != 1:
            raise ValueError(f"email {field} address is invalid")
        address = addresses[0]
        if any(ord(character) < 32 or character in "\r\n" for character in address):
            raise ValueError(f"email {field} address is invalid")
        local, separator, domain = address.rpartition("@")
        if not separator or not local or not domain or len(address) > MAX_INBOUND_ID_CHARS:
            raise ValueError(f"email {field} address is invalid")
        # Domains are case-insensitive; preserve the local part because some
        # installations intentionally distinguish it.
        return f"{local}@{domain.casefold()}"

    @staticmethod
    def _validate_mime_structure(message: Any) -> None:
        """Bound MIME traversal before selecting the text body."""
        parts = 0

        def visit(part: Any, depth: int = 0) -> None:
            nonlocal parts
            parts += 1
            if parts > MAX_EMAIL_MIME_PARTS or depth > MAX_EMAIL_MIME_DEPTH:
                raise ValueError("email MIME structure exceeds limit")
            children = getattr(part, "iter_parts", None)
            if children is None:
                return
            for child in children():
                visit(child, depth + 1)

        visit(message)


class WebhookInboundAdapter:
    channel: InboundChannel = "webhook"

    def parse(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        transport_id: str,
        secret: str,
        encryption_key: str | None = None,
        signing_key: str | None = None,
        account_id: str | None = None,
    ) -> InboundEnvelope:
        _verify_hmac(body, headers, secret, header="X-Noyra-Signature")
        payload = _json(body)
        return _common_envelope(
            channel=self.channel,
            transport_id=transport_id,
            event_id=payload.get("event_id") or payload.get("id"),
            account=payload.get("account_id") or payload.get("account") or "webhook",
            sender=payload.get("sender_id") or payload.get("sender"),
            conversation=payload.get("conversation_id") or payload.get("thread_id"),
            content=payload.get("content") or payload.get("text"),
            label=payload.get("sender_label") or payload.get("sender_name"),
            thread=payload.get("thread_id"),
            creator=bool(payload.get("creator"))
            if isinstance(payload.get("creator"), bool)
            else False,
        )


ADAPTERS: dict[InboundChannel, type[InboundAdapter]] = {
    "telegram": TelegramInboundAdapter,
    "qq": QQInboundAdapter,
    "feishu": FeishuInboundAdapter,
    "wechat": WeChatInboundAdapter,
    "email": EmailInboundAdapter,
    "webhook": WebhookInboundAdapter,
}


class InboundStore:
    """Durable inbound deduplication and external-account binding."""

    def __init__(self, database: Database, interactions: InteractionStore | None = None):
        self.database = database
        self.interactions = interactions or InteractionStore(database)

    def bind(
        self,
        subject_id: str,
        transport_id: str,
        *,
        external_account_id: str,
        external_sender_id: str,
        role: InboundRole = "participant",
        label: str = "",
    ) -> str:
        if role not in {"creator", "participant"}:
            raise ValueError("invalid inbound binding role")
        account = _text(external_account_id, "external_account_id")
        sender = _text(external_sender_id, "external_sender_id")
        label = _text(label, "binding label", required=False)
        binding_id = new_id("ibind")
        with self.database.transaction() as connection:
            transport = connection.execute(
                "SELECT channel FROM interaction_transports "
                "WHERE transport_id = ? AND subject_id = ? AND status = 'active'",
                (transport_id, subject_id),
            ).fetchone()
            if transport is None:
                raise ValueError("active transport is required for inbound binding")
            try:
                connection.execute(
                    "INSERT INTO interaction_bindings("
                    "binding_id, subject_id, transport_id, channel, external_account_id, "
                    "external_sender_id, role, label, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        binding_id,
                        subject_id,
                        transport_id,
                        transport["channel"],
                        account,
                        sender,
                        role,
                        label,
                        utc_now(),
                        utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("inbound account binding already exists") from error
        return binding_id

    def ingest(self, envelope: InboundEnvelope) -> InboundAccepted:
        now = utc_now()
        provider_duplicate = False
        with self.database.transaction() as connection:
            binding = connection.execute(
                "SELECT role, status FROM interaction_bindings "
                "WHERE subject_id = ? AND transport_id = ? AND channel = ? "
                "AND external_account_id = ? AND external_sender_id = ?",
                (
                    self._subject_for_transport(connection, envelope.transport_id),
                    envelope.transport_id,
                    envelope.channel,
                    envelope.external_account_id,
                    envelope.external_sender_id,
                ),
            ).fetchone()
            subject_id = self._subject_for_transport(connection, envelope.transport_id)
            if binding is None or binding["status"] != "active":
                raise PermissionError("inbound account is not bound")
            priority = 100 if binding["role"] == "creator" else 0
            event_id = new_id("inevt")
            content_digest = content_hash(envelope.content)
            try:
                connection.execute(
                    "INSERT INTO interaction_inbound_events("
                    "event_id, subject_id, transport_id, channel, provider_event_id, "
                    "external_account_id, external_sender_id, conversation_id, content_hash, "
                    "scheduling_priority, status, received_at, processed_at, interaction_id, "
                    "external_thread_id, reply_selector, reply_context_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, NULL, NULL, ?, ?, 1)",
                    (
                        event_id,
                        subject_id,
                        envelope.transport_id,
                        envelope.channel,
                        envelope.provider_event_id,
                        envelope.external_account_id,
                        envelope.external_sender_id,
                        envelope.conversation_id,
                        content_digest,
                        priority,
                        now,
                        envelope.thread_id,
                        envelope.reply_selector,
                    ),
                )
            except sqlite3.IntegrityError:
                provider_duplicate = True
                row = connection.execute(
                    "SELECT event_id, interaction_id, scheduling_priority, content_hash, status, "
                    "external_account_id, external_sender_id, conversation_id, channel, "
                    "external_thread_id, reply_selector, reply_context_version "
                    "FROM interaction_inbound_events WHERE subject_id = ? "
                    "AND transport_id = ? AND external_account_id = ? "
                    "AND provider_event_id = ?",
                    (
                        subject_id,
                        envelope.transport_id,
                        envelope.external_account_id,
                        envelope.provider_event_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("inbound event deduplication conflict") from None
                if (
                    row["content_hash"] != content_digest
                    or row["external_sender_id"] != envelope.external_sender_id
                    or row["external_account_id"] != envelope.external_account_id
                    or row["conversation_id"] != envelope.conversation_id
                    or row["channel"] != envelope.channel
                    or row["external_thread_id"] != envelope.thread_id
                    or row["reply_selector"] != envelope.reply_selector
                    or int(row["reply_context_version"]) != 1
                ):
                    raise ValueError("inbound event id identifies different content") from None
                if row["interaction_id"] is not None:
                    if row["status"] != "processed":
                        raise IntegrityError("inbound event linkage state is invalid") from None
                    return InboundAccepted(
                        str(row["event_id"]),
                        str(row["interaction_id"]),
                        True,
                        int(row["scheduling_priority"]),
                    )
                if row["status"] == "rejected":
                    reset = connection.execute(
                        "UPDATE interaction_inbound_events SET status = 'received', "
                        "processed_at = NULL WHERE event_id = ? AND status = 'rejected' "
                        "AND interaction_id IS NULL",
                        (row["event_id"],),
                    ).rowcount
                    if reset != 1:
                        raise IntegrityError("inbound rejected event retry raced") from None
                elif row["status"] != "received":
                    raise IntegrityError("inbound event status is invalid") from None
                event_id = str(row["event_id"])
                priority = int(row["scheduling_priority"])
            self._upsert_thread(
                connection,
                subject_id,
                envelope,
                now,
            )
        try:
            interaction = self.interactions.receive(
                subject_id,
                envelope.channel,
                (
                    envelope.external_sender_id
                    if envelope.channel == "email"
                    else envelope.conversation_id
                ),
                envelope.content,
                idempotency_key=(
                    "inbound:"
                    f"{envelope.transport_id}:"
                    f"{content_hash(envelope.external_account_id)[:32]}:"
                    f"{envelope.provider_event_id}"
                ),
            )
        except Exception:
            with self.database.transaction() as connection:
                rejected = connection.execute(
                    "UPDATE interaction_inbound_events SET status = 'rejected', "
                    "processed_at = ? WHERE event_id = ? AND status = 'received'",
                    (utc_now(), event_id),
                ).rowcount
                if rejected != 1:
                    concurrent = connection.execute(
                        "SELECT status, interaction_id, scheduling_priority "
                        "FROM interaction_inbound_events WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if (
                        concurrent is not None
                        and concurrent["status"] == "processed"
                        and concurrent["interaction_id"] is not None
                    ):
                        return InboundAccepted(
                            event_id,
                            str(concurrent["interaction_id"]),
                            True,
                            int(concurrent["scheduling_priority"]),
                        )
            raise
        with self.database.transaction() as connection:
            processed = connection.execute(
                "UPDATE interaction_inbound_events SET status = 'processed', "
                "processed_at = ?, interaction_id = ? "
                "WHERE event_id = ? AND status = 'received'",
                (utc_now(), interaction.interaction_id, event_id),
            ).rowcount
            if processed != 1:
                concurrent = connection.execute(
                    "SELECT status, interaction_id FROM interaction_inbound_events "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if (
                    concurrent is not None
                    and concurrent["status"] == "rejected"
                    and concurrent["interaction_id"] is None
                ):
                    # Another worker can fail while this worker has already
                    # committed the idempotent interaction.  Recover that
                    # narrow race in the same write transaction so a durable
                    # interaction is never left behind a rejected event.
                    reset = connection.execute(
                        "UPDATE interaction_inbound_events SET status = 'received', "
                        "processed_at = NULL WHERE event_id = ? AND status = 'rejected' "
                        "AND interaction_id IS NULL",
                        (event_id,),
                    ).rowcount
                    if reset == 1:
                        processed = connection.execute(
                            "UPDATE interaction_inbound_events SET status = 'processed', "
                            "processed_at = ?, interaction_id = ? "
                            "WHERE event_id = ? AND status = 'received' "
                            "AND interaction_id IS NULL",
                            (utc_now(), interaction.interaction_id, event_id),
                        ).rowcount
                    if processed == 1:
                        provider_duplicate = True
                        concurrent = connection.execute(
                            "SELECT status, interaction_id "
                            "FROM interaction_inbound_events WHERE event_id = ?",
                            (event_id,),
                        ).fetchone()
                if (
                    concurrent is None
                    or concurrent["status"] != "processed"
                    or concurrent["interaction_id"] != interaction.interaction_id
                ):
                    raise IntegrityError("inbound event processing state changed")
                provider_duplicate = True
        return InboundAccepted(
            event_id,
            interaction.interaction_id,
            provider_duplicate,
            priority,
        )

    def list_bindings(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT binding_id, transport_id, channel, external_account_id, "
                "external_sender_id, role, label, status, created_at, updated_at "
                "FROM interaction_bindings WHERE subject_id = ? "
                "ORDER BY channel, label, binding_id LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _upsert_thread(
        connection: Any,
        subject_id: str,
        envelope: InboundEnvelope,
        now: str,
    ) -> None:
        row = connection.execute(
            "SELECT thread_id FROM interaction_threads WHERE subject_id = ? "
            "AND transport_id = ? AND external_account_id = ? AND conversation_id = ? "
            "AND ((external_thread_id = ?) OR (external_thread_id IS NULL AND ? IS NULL))",
            (
                subject_id,
                envelope.transport_id,
                envelope.external_account_id,
                envelope.conversation_id,
                envelope.thread_id,
                envelope.thread_id,
            ),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO interaction_threads("
                "thread_id, subject_id, transport_id, channel, external_account_id, "
                "conversation_id, external_thread_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("ithread"),
                    subject_id,
                    envelope.transport_id,
                    envelope.channel,
                    envelope.external_account_id,
                    envelope.conversation_id,
                    envelope.thread_id,
                    now,
                    now,
                ),
            )
        else:
            connection.execute(
                "UPDATE interaction_threads SET updated_at = ? WHERE thread_id = ?",
                (now, row["thread_id"]),
            )

    def change_binding(
        self,
        binding_id: str,
        subject_id: str,
        *,
        status: Literal["active", "disabled", "revoked"],
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        if status not in {"active", "disabled", "revoked"}:
            raise ValueError("invalid inbound binding status")
        if not actor.strip() or not reason.strip() or len(reason) > 2_000:
            raise ValueError("binding status reason is required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_bindings WHERE binding_id = ? AND subject_id = ?",
                (binding_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"inbound binding not found: {binding_id}")
            if row["status"] == "revoked" and status != "revoked":
                raise ValueError("revoked inbound binding is terminal")
            connection.execute(
                "UPDATE interaction_bindings SET status = ?, updated_at = ? "
                "WHERE binding_id = ? AND subject_id = ?",
                (status, utc_now(), binding_id, subject_id),
            )
        return {
            "binding_id": binding_id,
            "transport_id": row["transport_id"],
            "channel": row["channel"],
            "status": status,
        }

    @staticmethod
    def _subject_for_transport(connection: Any, transport_id: str) -> str:
        row = connection.execute(
            "SELECT subject_id FROM interaction_transports WHERE transport_id = ?",
            (transport_id,),
        ).fetchone()
        if row is None:
            raise PermissionError("transport is not configured")
        return str(row["subject_id"])
