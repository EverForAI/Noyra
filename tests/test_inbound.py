from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction import (
    EmailInboundAdapter,
    FeishuInboundAdapter,
    InboundAuthenticationError,
    InboundChallenge,
    InboundEnvelope,
    InboundIgnored,
    InboundStore,
    InteractionRecord,
    InteractionStore,
    QQInboundAdapter,
    TransportInput,
    TransportRecord,
    TransportStore,
    WebhookInboundAdapter,
)
from noyra.interaction.types import InteractionKind
from noyra.model import ModelGateway
from noyra.service import NoyraService, ServiceSettings


def _setup(tmp_path: Path) -> tuple[SubjectKernel, TransportRecord, InboundStore]:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-inbound-tests",
        content_hash({"seed": "inbound"}),
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    transport = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="native webhook",
            endpoint="https://example.com/inbound",
            credentials={"webhook_secret": SecretStr("secret")},
        ),
        actor="operator",
    )
    store = InboundStore(kernel.database)
    store.bind(
        kernel.subject_id,
        transport.transport_id,
        external_account_id="account",
        external_sender_id="creator",
        role="creator",
    )
    return kernel, transport, store


def _envelope(
    transport_id: str, *, event: str = "event-1", content: str = "hello"
) -> InboundEnvelope:
    return InboundEnvelope(
        channel="webhook",
        transport_id=transport_id,
        provider_event_id=event,
        external_account_id="account",
        external_sender_id="creator",
        conversation_id="conversation",
        content=content,
    )


def test_native_inbound_envelope_requires_reply_selector() -> None:
    with pytest.raises(ValueError, match="reply selector"):
        InboundEnvelope(
            channel="qq",
            transport_id="transport",
            provider_event_id="event",
            external_account_id="app",
            external_sender_id="user",
            conversation_id="user",
            content="hello",
        )


def test_inbound_is_bound_deduplicated_threaded_and_creator_weighted(tmp_path: Path) -> None:
    kernel, transport, store = _setup(tmp_path)
    first = store.ingest(_envelope(transport.transport_id))
    duplicate = store.ingest(_envelope(transport.transport_id))
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert first.interaction_id == duplicate.interaction_id
    assert first.scheduling_priority == 100
    with kernel.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM interaction_threads").fetchone()[0] == 1
        assert (
            connection.execute("SELECT status FROM interaction_inbound_events").fetchone()[0]
            == "processed"
        )


def test_rejected_inbound_event_can_retry_without_creating_a_ghost_interaction(
    tmp_path: Path,
) -> None:
    kernel, transport, _ = _setup(tmp_path)

    class FailOnce(InteractionStore):
        calls = 0

        def receive(
            self,
            subject_id: str,
            channel: str,
            counterparty: str,
            content: str,
            *,
            kind: InteractionKind = "human_message",
            related_interaction_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> InteractionRecord:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient interaction failure")
            return super().receive(
                subject_id,
                channel,
                counterparty,
                content,
                kind=kind,
                related_interaction_id=related_interaction_id,
                idempotency_key=idempotency_key,
            )

    store = InboundStore(kernel.database, FailOnce(kernel.database))
    envelope = _envelope(transport.transport_id, event="retryable-event")
    with pytest.raises(RuntimeError, match="transient"):
        store.ingest(envelope)
    with kernel.database.connection() as connection:
        rejected = connection.execute(
            "SELECT status, interaction_id FROM interaction_inbound_events "
            "WHERE provider_event_id = 'retryable-event'"
        ).fetchone()
    assert tuple(rejected) == ("rejected", None)

    accepted = store.ingest(envelope)
    duplicate = store.ingest(envelope)
    assert accepted.duplicate is True
    assert duplicate.duplicate is True
    assert accepted.interaction_id == duplicate.interaction_id
    with kernel.database.connection() as connection:
        event = connection.execute(
            "SELECT status, interaction_id FROM interaction_inbound_events "
            "WHERE provider_event_id = 'retryable-event'"
        ).fetchone()
        interactions = connection.execute(
            "SELECT COUNT(*) FROM interactions WHERE interaction_id = ?",
            (accepted.interaction_id,),
        ).fetchone()[0]
    assert tuple(event) == ("processed", accepted.interaction_id)
    assert interactions == 1


def test_successful_inbound_recovers_rejected_state_from_concurrent_failure(
    tmp_path: Path,
) -> None:
    kernel, transport, _ = _setup(tmp_path)

    class RejectAfterCommit(InteractionStore):
        def receive(
            self,
            subject_id: str,
            channel: str,
            counterparty: str,
            content: str,
            *,
            kind: InteractionKind = "human_message",
            related_interaction_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> InteractionRecord:
            interaction = super().receive(
                subject_id,
                channel,
                counterparty,
                content,
                kind=kind,
                related_interaction_id=related_interaction_id,
                idempotency_key=idempotency_key,
            )
            with kernel.database.transaction() as connection:
                changed = connection.execute(
                    "UPDATE interaction_inbound_events SET status = 'rejected', "
                    "processed_at = '2026-08-25T00:00:00+00:00' "
                    "WHERE provider_event_id = 'concurrent-event' "
                    "AND status = 'received' AND interaction_id IS NULL"
                ).rowcount
            assert changed == 1
            return interaction

    store = InboundStore(kernel.database, RejectAfterCommit(kernel.database))
    accepted = store.ingest(_envelope(transport.transport_id, event="concurrent-event"))
    assert accepted.duplicate is True
    with kernel.database.connection() as connection:
        event = connection.execute(
            "SELECT status, interaction_id FROM interaction_inbound_events "
            "WHERE provider_event_id = 'concurrent-event'"
        ).fetchone()
        interactions = connection.execute(
            "SELECT COUNT(*) FROM interactions WHERE interaction_id = ?",
            (accepted.interaction_id,),
        ).fetchone()[0]
    assert tuple(event) == ("processed", accepted.interaction_id)
    assert interactions == 1


def test_inbound_replay_with_changed_content_is_rejected(tmp_path: Path) -> None:
    _, transport, store = _setup(tmp_path)
    store.ingest(_envelope(transport.transport_id))
    with pytest.raises(Exception, match=r"different|UNIQUE"):
        store.ingest(_envelope(transport.transport_id, content="tampered"))


def test_inbound_event_and_thread_identity_include_external_account(tmp_path: Path) -> None:
    kernel, transport, store = _setup(tmp_path)
    store.bind(
        kernel.subject_id,
        transport.transport_id,
        external_account_id="second-account",
        external_sender_id="creator",
        role="participant",
    )
    first = store.ingest(_envelope(transport.transport_id, event="same-provider-id"))
    second = store.ingest(
        InboundEnvelope(
            channel="webhook",
            transport_id=transport.transport_id,
            provider_event_id="same-provider-id",
            external_account_id="second-account",
            external_sender_id="creator",
            conversation_id="conversation",
            content="from second account",
        )
    )
    assert first.duplicate is False
    assert second.duplicate is False
    with kernel.database.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM interaction_threads").fetchone()[0] == 2


def test_creator_priority_is_used_by_interaction_scheduler(tmp_path: Path) -> None:
    kernel, transport, store = _setup(tmp_path)
    store.bind(
        kernel.subject_id,
        transport.transport_id,
        external_account_id="second-account",
        external_sender_id="participant",
        role="participant",
    )
    store.ingest(
        InboundEnvelope(
            channel="webhook",
            transport_id=transport.transport_id,
            provider_event_id="participant-event",
            external_account_id="second-account",
            external_sender_id="participant",
            conversation_id="participant-conversation",
            content="participant first",
        )
    )
    creator = store.ingest(_envelope(transport.transport_id, event="creator-event"))
    from noyra.cognition.interaction import InteractionCognition
    from noyra.cognition.settings import CognitionSettings

    cognition = InteractionCognition(
        kernel.database,
        kernel.subject_id,
        cast(ModelGateway, object()),
        CognitionSettings(interaction_cooldown_seconds=0),
    )
    due = cognition._due_interaction()
    assert due is not None
    assert due.interaction_id == creator.interaction_id


def test_inbound_identity_and_state_are_database_guarded(tmp_path: Path) -> None:
    kernel, transport, store = _setup(tmp_path)
    accepted = store.ingest(_envelope(transport.transport_id))

    def execute(sql: str) -> None:
        with kernel.database.transaction() as connection:
            connection.execute(
                sql,
                (accepted.event_id,),
            )

    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        execute(
            "UPDATE interaction_inbound_events SET conversation_id = 'tampered' WHERE event_id = ?"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        execute("DELETE FROM interaction_inbound_events WHERE event_id = ?")


def test_webhook_adapter_requires_fresh_hmac_and_never_forwards_extra_fields() -> None:
    payload = {
        "event_id": "event-1",
        "account_id": "account",
        "sender_id": "sender",
        "conversation_id": "conversation",
        "content": "hello",
        "command": "do-not-forward",
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "nonce"
    signature = hmac.new(
        b"secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    envelope = WebhookInboundAdapter().parse(
        body,
        {
            "X-Noyra-Timestamp": timestamp,
            "X-Noyra-Nonce": nonce,
            "X-Noyra-Signature": signature,
        },
        transport_id="transport-1",
        secret="secret",
    )
    assert envelope.content == "hello"
    with pytest.raises(ValueError, match="stale"):
        WebhookInboundAdapter().parse(
            body,
            {
                "X-Noyra-Timestamp": "1",
                "X-Noyra-Nonce": nonce,
                "X-Noyra-Signature": signature,
            },
            transport_id="transport-1",
            secret="secret",
        )


def test_feishu_signature_and_challenge_are_platform_specific() -> None:
    challenge = json.dumps(
        {"type": "url_verification", "token": "secret", "challenge": "c"}
    ).encode()
    result = FeishuInboundAdapter().parse(
        challenge, {}, transport_id="transport-1", secret="secret"
    )
    assert isinstance(result, InboundChallenge)
    assert result.challenge == "c"


def test_feishu_event_signature_uses_encrypt_key_not_verification_token() -> None:
    from noyra.interaction import FeishuInboundAdapter

    payload = {
        "header": {
            "event_id": "event-1",
            "app_id": "cli_app",
            "token": "verification-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou-user"}},
            "message": {"chat_id": "oc-chat", "content": '{"text":"hello"}'},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "lark-nonce"
    encrypt_key = "lark-encrypt-key"
    signature = hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}".encode() + body).hexdigest()
    envelope = FeishuInboundAdapter().parse(
        body,
        {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
        transport_id="transport-1",
        secret="verification-token",
        encryption_key=encrypt_key,
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.external_account_id == "cli_app"
    assert envelope.content == "hello"
    assert envelope.reply_selector == "feishu:chat_id"


def test_feishu_plaintext_event_uses_verification_token_without_encrypt_key() -> None:
    payload = {
        "header": {
            "event_id": "event-plain",
            "app_id": "cli_app",
            "token": "verification-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou-user"}},
            "message": {"chat_id": "oc-chat", "content": '{"text":"hello"}'},
        },
    }
    envelope = FeishuInboundAdapter().parse(
        json.dumps(payload, separators=(",", ":")).encode(),
        {},
        transport_id="transport-1",
        secret="verification-token",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.provider_event_id == "event-plain"


def test_feishu_legacy_v1_event_uses_uuid_and_top_level_app_id() -> None:
    payload = {
        "uuid": "legacy-event-1",
        "token": "verification-token",
        "type": "event_callback",
        "app_id": "cli_legacy",
        "event": {
            "sender": {"sender_id": {"open_id": "ou-user"}},
            "message": {"chat_id": "oc-chat", "content": '{"text":"hello"}'},
        },
    }
    envelope = FeishuInboundAdapter().parse(
        json.dumps(payload, separators=(",", ":")).encode(),
        {},
        transport_id="transport-1",
        secret="verification-token",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.provider_event_id == "legacy-event-1"
    assert envelope.external_account_id == "cli_legacy"


def test_feishu_encrypted_event_is_decrypted_before_parsing() -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encrypt_key = "lark-encrypt-key"
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "event-encrypted",
            "app_id": "cli_app",
            "token": "verification-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou-user"}},
            "message": {"chat_id": "oc-chat", "content": '{"text":"secret hello"}'},
        },
    }
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    padding = 16 - len(plaintext) % 16
    padded = plaintext + bytes([padding]) * padding
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
    body = json.dumps({"encrypt": encrypted}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "lark-nonce"
    signature = hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}".encode() + body).hexdigest()
    envelope = FeishuInboundAdapter().parse(
        body,
        {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
        transport_id="transport-1",
        secret="verification-token",
        encryption_key=encrypt_key,
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.provider_event_id == "event-encrypted"
    assert envelope.content == "secret hello"


def test_qq_c2c_event_uses_sender_as_conversation_and_message_as_reply_target() -> None:
    from noyra.interaction import QQInboundAdapter

    payload = {
        "id": "qq-message-1",
        "author": {"user_openid": "qq-user-1", "username": "Alice"},
        "content": "hello",
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "qq-nonce"
    signature = hmac.new(
        b"secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    envelope = QQInboundAdapter().parse(
        body,
        {
            "X-Noyra-Timestamp": timestamp,
            "X-Noyra-Nonce": nonce,
            "X-Noyra-Signature": signature,
        },
        transport_id="transport-1",
        secret="secret",
        signing_key="secret",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.conversation_id == "qq-user-1"
    assert envelope.thread_id == "qq-message-1"
    assert envelope.reply_selector == "qq:user"


@pytest.mark.parametrize(
    ("event", "expected_sender", "expected_target", "expected_selector"),
    [
        (
            {"id": "legacy-c2c", "author": {"id": "legacy-user"}, "content": "hi"},
            "legacy-user",
            "legacy-user",
            "qq:user",
        ),
        (
            {
                "id": "legacy-group",
                "author": {"id": "legacy-member"},
                "group_id": "legacy-group-id",
                "content": "hi",
            },
            "legacy-member",
            "legacy-group-id",
            "qq:group",
        ),
    ],
)
def test_qq_legacy_hmac_route_accepts_official_id_aliases(
    event: dict[str, object],
    expected_sender: str,
    expected_target: str,
    expected_selector: str,
) -> None:
    body = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "qq-legacy-route-nonce"
    signature = hmac.new(
        b"legacy-callback-secret",
        f"{timestamp}.{nonce}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    envelope = QQInboundAdapter().parse(
        body,
        {
            "X-Noyra-Timestamp": timestamp,
            "X-Noyra-Nonce": nonce,
            "X-Noyra-Signature": signature,
        },
        transport_id="transport-1",
        secret="app-secret",
        signing_key="legacy-callback-secret",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.external_sender_id == expected_sender
    assert envelope.conversation_id == expected_target
    assert envelope.reply_selector == expected_selector


@pytest.mark.parametrize(
    ("event_type", "event", "expected_sender", "expected_target", "expected_selector"),
    [
        (
            "C2C_MESSAGE_CREATE",
            {"author": {"id": "legacy-user"}},
            "legacy-user",
            "legacy-user",
            "qq:user",
        ),
        (
            "GROUP_MESSAGE_CREATE",
            {"author": {"member_openid": "member-1"}, "group_openid": "group-1"},
            "member-1",
            "group-1",
            "qq:group",
        ),
        (
            "GROUP_AT_MESSAGE_CREATE",
            {"author": {"id": "legacy-member"}, "group_id": "legacy-group"},
            "legacy-member",
            "legacy-group",
            "qq:group",
        ),
        (
            "AT_MESSAGE_CREATE",
            {"author": {"id": "guild-user"}, "channel_id": "channel-1"},
            "guild-user",
            "channel-1",
            "qq:channel",
        ),
        (
            "DIRECT_MESSAGE_CREATE",
            {"author": {"id": "guild-user"}, "guild_id": "guild-1"},
            "guild-user",
            "guild-1",
            "qq:dm",
        ),
    ],
)
def test_qq_official_message_types_preserve_native_reply_route(
    event_type: str,
    event: dict[str, object],
    expected_sender: str,
    expected_target: str,
    expected_selector: str,
) -> None:
    message = {
        "id": f"message-{event_type}",
        "content": "hello",
        "message_reference": {"message_id": "quoted-old-message"},
        **event,
    }
    body = json.dumps({"op": 0, "t": event_type, "d": message}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "qq-route-nonce"
    signature = hmac.new(
        b"legacy-callback-secret",
        f"{timestamp}.{nonce}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    envelope = QQInboundAdapter().parse(
        body,
        {
            "X-Noyra-Timestamp": timestamp,
            "X-Noyra-Nonce": nonce,
            "X-Noyra-Signature": signature,
        },
        transport_id="transport-1",
        secret="app-secret",
        signing_key="legacy-callback-secret",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.external_sender_id == expected_sender
    assert envelope.conversation_id == expected_target
    assert envelope.reply_selector == expected_selector
    assert envelope.thread_id == f"message-{event_type}"


def test_qq_official_ed25519_callback_and_validation_handshake() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    app_secret = "qq-app-secret"
    private_key = Ed25519PrivateKey.from_private_bytes((app_secret.encode() * 4)[:32])
    timestamp = str(int(time.time()))
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "qq-official-message",
            "author": {"user_openid": "qq-user-1", "username": "Alice"},
            "content": "hello",
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": private_key.sign(timestamp.encode() + body).hex(),
    }
    envelope = QQInboundAdapter().parse(
        body, headers, transport_id="transport-1", secret=app_secret
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.content == "hello"

    validation = {
        "op": 13,
        "d": {"plain_token": "plain-token", "event_ts": timestamp},
    }
    validation_body = json.dumps(validation, separators=(",", ":")).encode()
    challenge = QQInboundAdapter().parse(
        validation_body,
        {},
        transport_id="transport-1",
        secret=app_secret,
    )
    assert isinstance(challenge, InboundChallenge)
    assert challenge.response == {
        "plain_token": "plain-token",
        "signature": private_key.sign((timestamp + "plain-token").encode()).hex(),
    }


def test_qq_legacy_hmac_requires_an_explicit_legacy_secret() -> None:
    body = b'{"id":"qq-message","author":{"user_openid":"qq-user"},"content":"hello"}'
    timestamp = str(int(time.time()))
    nonce = "nonce"
    signature = hmac.new(
        b"secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Noyra-Timestamp": timestamp,
        "X-Noyra-Nonce": nonce,
        "X-Noyra-Signature": signature,
    }
    with pytest.raises(InboundAuthenticationError):
        QQInboundAdapter().parse(body, headers, transport_id="transport-1", secret="secret")
    envelope = QQInboundAdapter().parse(
        body,
        headers,
        transport_id="transport-1",
        secret="public-key",
        signing_key="secret",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.content == "hello"


def test_authenticated_non_text_events_are_acknowledged_and_ignored() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    app_secret = "qq-app-secret"
    private_key = Ed25519PrivateKey.from_private_bytes((app_secret.encode() * 4)[:32])
    timestamp = str(int(time.time()))
    body = json.dumps(
        {"op": 0, "t": "GUILD_CREATE", "d": {"id": "guild-1"}},
        separators=(",", ":"),
    ).encode()
    result = QQInboundAdapter().parse(
        body,
        {
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": private_key.sign(timestamp.encode() + body).hex(),
        },
        transport_id="transport-1",
        secret=app_secret,
    )
    assert isinstance(result, InboundIgnored)

    feishu = {
        "header": {
            "event_id": "event-1",
            "event_type": "im.chat.member bot.added_v1",
            "token": "verification-token",
        },
        "event": {},
    }
    result = FeishuInboundAdapter().parse(
        json.dumps(feishu, separators=(",", ":")).encode(),
        {},
        transport_id="transport-1",
        secret="verification-token",
    )
    assert isinstance(result, InboundIgnored)


def test_wechat_xml_rejects_dtd_and_entity_declarations() -> None:
    body = b"<!DOCTYPE xml [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><xml/>"
    with pytest.raises(ValueError, match="XML is invalid"):
        from noyra.interaction import WeChatInboundAdapter

        WeChatInboundAdapter().parse(
            body,
            {
                "X-Wechat-Signature": "bad",
                "X-Wechat-Timestamp": str(int(time.time())),
                "X-Wechat-Nonce": "nonce",
            },
            transport_id="transport-1",
            secret="secret",
        )


def test_wechat_plaintext_cdata_callback_is_accepted() -> None:
    from noyra.interaction import WeChatInboundAdapter

    body = (
        b"<xml>"
        b"<ToUserName><![CDATA[wx-account]]></ToUserName>"
        b"<FromUserName><![CDATA[user-openid]]></FromUserName>"
        b"<MsgType><![CDATA[text]]></MsgType>"
        b"<Content><![CDATA[hello <!DOCTYPE is just text>]]></Content>"
        b"<MsgId>message-1</MsgId>"
        b"</xml>"
    )
    timestamp = str(int(time.time()))
    nonce = "nonce"
    signature = hashlib.sha1("".join(sorted(("token", timestamp, nonce))).encode()).hexdigest()
    envelope = WeChatInboundAdapter().parse(
        body,
        {
            "X-Wechat-Signature": signature,
            "X-Wechat-Timestamp": timestamp,
            "X-Wechat-Nonce": nonce,
        },
        transport_id="transport-1",
        secret="token",
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.external_account_id == "wx-account"
    assert envelope.external_sender_id == "user-openid"
    assert envelope.content == "hello <!DOCTYPE is just text>"


def test_wechat_aes_callback_is_decrypted_and_cdata_is_preserved() -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from noyra.interaction import WeChatInboundAdapter

    key = os.urandom(32)
    encoding_key = base64.b64encode(key).decode("ascii").rstrip("=")
    app_id = "wx-account"
    message = (
        b"<xml>"
        b"<ToUserName><![CDATA[wx-account]]></ToUserName>"
        b"<FromUserName><![CDATA[user-openid]]></FromUserName>"
        b"<MsgType><![CDATA[text]]></MsgType>"
        b"<Content><![CDATA[encrypted hello]]></Content>"
        b"<MsgId>message-2</MsgId>"
        b"</xml>"
    )
    plaintext = os.urandom(16) + len(message).to_bytes(4, "big") + message + app_id.encode()
    padding = 16 - len(plaintext) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = encryptor.update(plaintext + bytes([padding]) * padding) + encryptor.finalize()
    encrypted_text = base64.b64encode(encrypted).decode("ascii")
    timestamp = str(int(time.time()))
    nonce = "nonce"
    signature = hashlib.sha1(
        "".join(sorted(("token", timestamp, nonce, encrypted_text))).encode()
    ).hexdigest()
    body = (
        f"<xml><Encrypt><![CDATA[{encrypted_text}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp><Nonce><![CDATA[{nonce}]]></Nonce></xml>"
    ).encode()
    envelope = WeChatInboundAdapter().parse(
        body,
        {
            "X-Wechat-MsgSignature": signature,
            "X-Wechat-Timestamp": timestamp,
            "X-Wechat-Nonce": nonce,
        },
        transport_id="transport-1",
        secret="token",
        encryption_key=encoding_key,
        account_id=app_id,
    )
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.provider_event_id == "message-2"
    assert envelope.content == "encrypted hello"

    stale_timestamp = "1"
    stale_signature = hashlib.sha1(
        "".join(sorted(("token", stale_timestamp, nonce, encrypted_text))).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="stale"):
        WeChatInboundAdapter().parse(
            body,
            {
                "X-Wechat-MsgSignature": stale_signature,
                "X-Wechat-Timestamp": stale_timestamp,
                "X-Wechat-Nonce": nonce,
            },
            transport_id="transport-1",
            secret="token",
            encryption_key=encoding_key,
            account_id=app_id,
        )

    with pytest.raises(ValueError, match="account identity"):
        WeChatInboundAdapter().parse(
            body,
            {
                "X-Wechat-MsgSignature": signature,
                "X-Wechat-Timestamp": timestamp,
                "X-Wechat-Nonce": nonce,
            },
            transport_id="transport-1",
            secret="token",
            encryption_key=encoding_key,
            account_id="different-app-id",
        )


def _signed_email(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = "email-nonce"
    signature = hmac.new(
        b"secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Noyra-Timestamp": timestamp,
        "X-Noyra-Nonce": nonce,
        "X-Noyra-Signature": signature,
    }


def test_email_adapter_canonicalizes_display_name_mailboxes() -> None:
    body = (
        b"From: Alice <User@Example.COM>\r\n"
        b"To: Noyra <Bot@Example.COM>\r\n"
        b"Message-ID: <message-1@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"hello\r\n"
    )
    envelope = EmailInboundAdapter().parse(
        body,
        _signed_email(body),
        transport_id="transport-1",
        secret="secret",
    )
    assert envelope.external_sender_id == "User@example.com"
    assert envelope.external_account_id == "Bot@example.com"
    assert envelope.content == "hello"


def test_email_adapter_rejects_ambiguous_recipient_and_mime_fanout() -> None:
    ambiguous = (
        b"From: user@example.com\r\n"
        b"To: one@example.com, two@example.com\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello\r\n"
    )
    with pytest.raises(ValueError, match="recipient address is invalid"):
        EmailInboundAdapter().parse(
            ambiguous,
            _signed_email(ambiguous),
            transport_id="transport-1",
            secret="secret",
        )

    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "user@example.com"
    message["To"] = "bot@example.com"
    message.set_content("hello")
    for index in range(256):
        message.add_attachment(
            f"part-{index}".encode(), maintype="application", subtype="octet-stream"
        )
    body = message.as_bytes()
    with pytest.raises(ValueError, match="MIME structure exceeds limit"):
        EmailInboundAdapter().parse(
            body,
            _signed_email(body),
            transport_id="transport-1",
            secret="secret",
        )


def test_native_http_callbacks_use_platform_handshakes_and_acknowledgements(
    tmp_path: Path,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    subject_id = "Noyra-native-http-acks"
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id=subject_id,
            genesis_hash=content_hash({"subject": subject_id}),
            host="127.0.0.1",
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    app_secret = "qq-app-secret"
    qq = service.http.transports.configure(
        subject_id,
        TransportInput(
            channel="qq",
            label="official QQ",
            endpoint="https://api.sgroup.qq.com",
            credentials={
                "app_id": SecretStr("qq-app-id"),
                "app_secret": SecretStr(app_secret),
            },
        ),
        actor="operator",
    )
    service.http.inbound.bind(
        subject_id,
        qq.transport_id,
        external_account_id="qq-app-id",
        external_sender_id="qq-user",
        role="participant",
    )
    wechat = service.http.transports.configure(
        subject_id,
        TransportInput(
            channel="wechat",
            label="official account",
            endpoint="https://api.weixin.qq.com",
            credentials={
                "app_id": SecretStr("wx-app-id"),
                "app_secret": SecretStr("wx-app-secret"),
                "webhook_secret": SecretStr("wx-token"),
            },
        ),
        actor="operator",
    )
    service.http.inbound.bind(
        subject_id,
        wechat.transport_id,
        external_account_id="wx-account",
        external_sender_id="wx-user",
        role="participant",
    )
    service.http.start()
    try:
        _, port = service.http.address
        qq_url = f"http://127.0.0.1:{port}/api/v1/webhooks/qq/{qq.transport_id}"
        timestamp = str(int(time.time()))
        validation_body = json.dumps(
            {
                "op": 13,
                "d": {"plain_token": "plain-token", "event_ts": timestamp},
            },
            separators=(",", ":"),
        ).encode()
        with urlopen(
            Request(
                qq_url,
                data=validation_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ) as response:
            validation = json.loads(response.read())
        private_key = Ed25519PrivateKey.from_private_bytes((app_secret.encode() * 4)[:32])
        assert validation == {
            "plain_token": "plain-token",
            "signature": private_key.sign((timestamp + "plain-token").encode()).hex(),
        }

        qq_body = json.dumps(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "qq-http-message",
                    "author": {"user_openid": "qq-user"},
                    "content": "hello from QQ",
                },
            },
            separators=(",", ":"),
        ).encode()
        qq_headers = {
            "Content-Type": "application/json",
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": private_key.sign(timestamp.encode() + qq_body).hex(),
        }
        with urlopen(
            Request(qq_url, data=qq_body, headers=qq_headers, method="POST"),
            timeout=5,
        ) as response:
            assert json.loads(response.read()) == {"op": 12, "d": 0}

        nonce = "wx-nonce"
        wx_timestamp = str(int(time.time()))
        wx_signature = hashlib.sha1(
            "".join(sorted(("wx-token", wx_timestamp, nonce))).encode()
        ).hexdigest()
        wx_body = (
            b"<xml><ToUserName>wx-account</ToUserName>"
            b"<FromUserName>wx-user</FromUserName>"
            b"<MsgType>text</MsgType><Content>hello from WeChat</Content>"
            b"<MsgId>wx-http-message</MsgId></xml>"
        )
        # Dispatch uses the normalized path, while signature verification must
        # retain the original query-bearing request target.
        wx_url = (
            f"http://127.0.0.1:{port}/api/v1/webhooks/wechat/{wechat.transport_id}"
            f"?signature={wx_signature}&timestamp={wx_timestamp}&nonce={nonce}"
        )
        with urlopen(
            Request(
                wx_url,
                data=wx_body,
                headers={"Content-Type": "application/xml"},
                method="POST",
            ),
            timeout=5,
        ) as response:
            assert response.read() == b"success"

        with service.kernel.database.connection() as connection:
            recorded = connection.execute(
                "SELECT provider_event_id, status FROM interaction_inbound_events "
                "WHERE provider_event_id IN (?, ?) ORDER BY provider_event_id",
                ("qq-http-message", "wx-http-message"),
            ).fetchall()
        assert [tuple(row) for row in recorded] == [
            ("qq-http-message", "processed"),
            ("wx-http-message", "processed"),
        ]
    finally:
        service.http.close()
        service.kernel.close()
