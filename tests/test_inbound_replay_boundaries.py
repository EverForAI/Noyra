from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import SecretStr

from noyra.core import Database, SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction import (
    InboundAccepted,
    InboundAuthenticationError,
    InboundEnvelope,
    InboundStore,
    TransportInput,
    TransportStore,
)
from noyra.interaction.inbound import ADAPTERS, InboundChannel, WeChatInboundAdapter
from noyra.service import NoyraService, ServiceSettings
from support.inbound_callbacks import encrypted_wechat_callback

KEY = bytes(range(32))
ACCOUNT = "synthetic-wx-account"
TOKEN = "synthetic-callback-secret"
MESSAGE = (
    b"<xml><ToUserName>synthetic-wx-account</ToUserName>"
    b"<FromUserName>synthetic-sender</FromUserName><MsgType>text</MsgType>"
    b"<Content><![CDATA[synthetic hello <!DOCTYPE is only text>]]></Content>"
    b"<MsgId>synthetic-message</MsgId></xml>"
)


def plaintext_headers() -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = "synthetic-captured-nonce"
    signature = hashlib.sha1("".join(sorted((TOKEN, timestamp, nonce))).encode()).hexdigest()
    return {
        "X-Wechat-Signature": signature,
        "X-Wechat-Timestamp": timestamp,
        "X-Wechat-Nonce": nonce,
    }


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("wire_format", ["xml", "json"])
def test_wechat_captured_plaintext_signature_cannot_admit_an_altered_body(
    configured: bool, wire_format: str
) -> None:
    headers = plaintext_headers()
    if wire_format == "xml":
        original = MESSAGE
        changed = MESSAGE.replace(b"synthetic-message", b"forged-message").replace(
            b"synthetic hello", b"forged invitation"
        )
    else:
        original = json.dumps(
            {"msgid": "original", "to": ACCOUNT, "from": "synthetic-sender", "content": "hello"}
        ).encode()
        changed = original.replace(b"original", b"forged").replace(b"hello", b"forged text")
    for body in (original, changed):
        with pytest.raises(InboundAuthenticationError, match="encrypted"):
            WeChatInboundAdapter().parse(
                body,
                headers,
                transport_id="synthetic-transport",
                secret=TOKEN,
                encryption_key=base64.b64encode(KEY).decode().rstrip("=") if configured else None,
                account_id=ACCOUNT if configured else None,
            )


@dataclass(frozen=True)
class Wire:
    channel: InboundChannel
    body: bytes
    headers: dict[str, str]
    arguments: dict[str, Any]

    def parse(self, transport_id: str) -> InboundEnvelope:
        result = ADAPTERS[self.channel]().parse(
            self.body, self.headers, transport_id=transport_id, **self.arguments
        )
        assert isinstance(result, InboundEnvelope)
        return result


def wire(kind: str) -> Wire:
    timestamp = str(int(time.time()))
    nonce = "synthetic-nonce"
    channel: InboundChannel
    if kind == "wechat":
        body, headers = encrypted_wechat_callback(MESSAGE, key=KEY, token=TOKEN, account_id=ACCOUNT)
        return Wire(
            "wechat",
            body,
            headers,
            {
                "secret": TOKEN,
                "encryption_key": base64.b64encode(KEY).decode().rstrip("="),
                "account_id": ACCOUNT,
            },
        )
    if kind == "telegram":
        return Wire(
            "telegram",
            json.dumps(
                {
                    "update_id": 123,
                    "message": {
                        "chat": {"id": 456},
                        "from": {"id": 789},
                        "text": "synthetic invitation",
                    },
                }
            ).encode(),
            {"X-Telegram-Bot-Api-Secret-Token": TOKEN},
            {"secret": TOKEN},
        )
    if kind.startswith("feishu"):
        body = json.dumps(
            {
                "header": {
                    "event_id": "synthetic-event",
                    "app_id": "synthetic-app",
                    "token": TOKEN,
                    "event_type": "im.message.receive_v1",
                },
                "event": {
                    "sender": {"sender_id": {"open_id": "synthetic-sender"}},
                    "message": {
                        "message_id": "synthetic-message",
                        "chat_id": "synthetic-chat",
                        "message_type": "text",
                        "content": json.dumps({"text": "synthetic invitation"}),
                    },
                },
            }
        ).encode()
        headers = {}
        arguments = {"secret": TOKEN}
        if kind in {"feishu-signed", "feishu-encrypted"}:
            key = "synthetic-feishu-encrypt-key"
            if kind == "feishu-encrypted":
                iv = bytes(range(16))
                padding = 16 - len(body) % 16
                encryptor = Cipher(
                    algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)
                ).encryptor()
                ciphertext = (
                    encryptor.update(body + bytes([padding]) * padding) + encryptor.finalize()
                )
                body = json.dumps({"encrypt": base64.b64encode(iv + ciphertext).decode()}).encode()
            headers = {
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": hashlib.sha256(
                    f"{timestamp}{nonce}{key}".encode() + body
                ).hexdigest(),
            }
            arguments["encryption_key"] = key
        return Wire("feishu", body, headers, arguments)
    if kind in {"qq", "qq-hmac"}:
        body = json.dumps(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "synthetic-message",
                    "author": {"user_openid": "synthetic-sender"},
                    "content": "synthetic invitation",
                },
            }
        ).encode()
        if kind == "qq":
            source = TOKEN.encode()
            while len(source) < 32:
                source += source
            private = Ed25519PrivateKey.from_private_bytes(source[:32])
            return Wire(
                "qq",
                body,
                {
                    "X-Signature-Timestamp": timestamp,
                    "X-Signature-Ed25519": private.sign(timestamp.encode() + body).hex(),
                },
                {"secret": TOKEN},
            )
        channel = "qq"
        arguments = {"secret": TOKEN, "signing_key": TOKEN}
    elif kind == "email":
        body = (
            b"From: sender@example.invalid\r\nTo: bot@example.invalid\r\n"
            b"Message-ID: <synthetic-message@example.invalid>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nsynthetic invitation\r\n"
        )
        channel, arguments = "email", {"secret": TOKEN}
    else:
        assert kind == "webhook"
        body = json.dumps(
            {
                "event_id": "synthetic-message",
                "account_id": "synthetic-account",
                "sender_id": "synthetic-sender",
                "conversation_id": "synthetic-chat",
                "content": "synthetic invitation",
            }
        ).encode()
        channel, arguments = "webhook", {"secret": TOKEN}
    digest = hmac.new(
        TOKEN.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return Wire(
        channel,
        body,
        {"X-Noyra-Timestamp": timestamp, "X-Noyra-Nonce": nonce, "X-Noyra-Signature": digest},
        arguments,
    )


KINDS = [
    "telegram",
    "feishu-token",
    "feishu-signed",
    "feishu-encrypted",
    "qq",
    "qq-hmac",
    "email",
    "webhook",
    "wechat",
]


def setup(tmp_path: Path, callback: Wire) -> tuple[SubjectKernel, InboundEnvelope]:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "M17-Synthetic", content_hash("M17"))
    endpoint = {
        "telegram": "https://api.telegram.org",
        "feishu": "https://open.feishu.cn",
        "qq": "https://api.sgroup.qq.com",
        "wechat": "https://api.weixin.qq.com",
        "email": "smtps://smtp.example.invalid",
        "webhook": "https://example.invalid/inbound",
    }[callback.channel]
    transport = TransportStore(kernel.database, tmp_path / "secrets").configure(
        kernel.subject_id,
        TransportInput(
            channel=callback.channel,
            label="Synthetic replay boundary",
            endpoint=endpoint,
            credentials={"webhook_secret": SecretStr(TOKEN)},
        ),
        actor="operator",
    )
    envelope = callback.parse(transport.transport_id)
    InboundStore(kernel.database).bind(
        kernel.subject_id,
        transport.transport_id,
        external_account_id=envelope.external_account_id,
        external_sender_id=envelope.external_sender_id,
        role="participant",
    )
    return kernel, envelope


def snapshot(database: Database) -> dict[str, list[tuple[Any, ...]]]:
    with database.read_transaction() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")
            ]
            for table in [
                "interaction_inbound_events",
                "interactions",
                "interaction_decisions",
                "events",
                "model_calls",
                "model_attempts",
            ]
        }


@pytest.mark.parametrize("kind", KINDS)
def test_authenticated_identical_callback_is_acknowledged_once_across_store_restart(
    tmp_path: Path, kind: str
) -> None:
    callback = wire(kind)
    kernel, envelope = setup(tmp_path, callback)
    try:
        store = InboundStore(kernel.database)
        first = store.ingest(envelope)
        assert isinstance(first, InboundAccepted)
        frozen = snapshot(kernel.database)
        assert not first.duplicate
        for _ in range(3):
            replay = callback.parse(envelope.transport_id)
            duplicate = InboundStore(Database(kernel.database.path)).ingest(replay)
            assert isinstance(duplicate, InboundAccepted)
            assert duplicate.duplicate
            assert (duplicate.event_id, duplicate.interaction_id) == (
                first.event_id,
                first.interaction_id,
            )
            assert snapshot(kernel.database) == frozen
        assert len(frozen["interaction_inbound_events"]) == len(frozen["interactions"]) == 1
        assert not frozen["model_calls"] and not frozen["model_attempts"]
    finally:
        kernel.close()


@pytest.mark.parametrize("kind", ["webhook", "qq", "wechat"])
def test_freshly_authenticated_provider_retry_preserves_original_business_event(
    tmp_path: Path, kind: str
) -> None:
    callback = wire(kind)
    kernel, envelope = setup(tmp_path, callback)
    try:
        first = InboundStore(kernel.database).ingest(envelope)
        assert isinstance(first, InboundAccepted)
        frozen = snapshot(kernel.database)
        # New authentication time (and new WeChat ciphertext) is a provider
        # retry, not a new invitation or a reason to bypass persistent identity.
        with patch("time.time", return_value=time.time() + 1):
            retry = wire(kind)
            assert retry.headers != callback.headers
            accepted = InboundStore(Database(kernel.database.path)).ingest(
                retry.parse(envelope.transport_id)
            )
        assert isinstance(accepted, InboundAccepted) and accepted.duplicate
        assert accepted.interaction_id == first.interaction_id
        assert snapshot(kernel.database) == frozen
    finally:
        kernel.close()


@pytest.mark.parametrize("kind", ["webhook", "email", "qq", "qq-hmac", "feishu-signed", "wechat"])
def test_signature_replay_outside_authentication_window_is_rejected(kind: str) -> None:
    callback = wire(kind)
    now = time.time()
    with (
        patch("time.time", return_value=now + 301),
        pytest.raises(
            InboundAuthenticationError,
            match="timestamp is invalid" if kind == "qq" else "stale",
        ),
    ):
        callback.parse("synthetic-transport")


@pytest.mark.parametrize("kind", ["webhook", "wechat"])
def test_retry_after_local_failure_recovers_the_existing_inbound_event(
    tmp_path: Path, kind: str
) -> None:
    callback = wire(kind)
    kernel, envelope = setup(tmp_path, callback)
    try:
        store = InboundStore(kernel.database)
        with (
            patch.object(
                store.interactions, "receive", side_effect=RuntimeError("synthetic crash")
            ),
            pytest.raises(RuntimeError, match="synthetic crash"),
        ):
            store.ingest(envelope)
        before = snapshot(kernel.database)
        assert len(before["interaction_inbound_events"]) == 1
        assert not before["interactions"]
        recovered = InboundStore(Database(kernel.database.path)).ingest(
            callback.parse(envelope.transport_id)
        )
        assert isinstance(recovered, InboundAccepted) and recovered.duplicate
        frozen = snapshot(kernel.database)
        again = InboundStore(Database(kernel.database.path)).ingest(envelope)
        assert isinstance(again, InboundAccepted) and again.duplicate
        assert again.interaction_id == recovered.interaction_id
        assert len(frozen["interactions"]) == len(frozen["interaction_inbound_events"]) == 1
        assert snapshot(kernel.database) == frozen
    finally:
        kernel.close()


def test_wechat_http_requires_encrypted_messages_and_acknowledges_legitimate_retries(
    tmp_path: Path,
) -> None:
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id="M17-HTTP-Synthetic",
            genesis_hash=content_hash("M17-HTTP"),
            host="127.0.0.1",
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    transport = service.http.transports.configure(
        service.kernel.subject_id,
        TransportInput(
            channel="wechat",
            label="Synthetic encrypted callback",
            endpoint="https://api.weixin.qq.com",
            credentials={
                "app_id": SecretStr(ACCOUNT),
                "webhook_secret": SecretStr(TOKEN),
                "encoding_aes_key": SecretStr(base64.b64encode(KEY).decode().rstrip("=")),
            },
        ),
        actor="operator",
    )
    service.http.inbound.bind(
        service.kernel.subject_id,
        transport.transport_id,
        external_account_id=ACCOUNT,
        external_sender_id="synthetic-sender",
        role="participant",
    )
    service.http.start()
    try:
        _, port = service.http.address
        callback = wire("wechat")
        query = urlencode(
            {
                "msg_signature": callback.headers["X-Wechat-MsgSignature"],
                "timestamp": callback.headers["X-Wechat-Timestamp"],
                "nonce": callback.headers["X-Wechat-Nonce"],
                "encrypt_type": "aes",
            }
        )
        frozen = None
        for prefix in ["/webhooks/", "/api/v1/webhooks/"]:
            base = f"http://127.0.0.1:{port}{prefix}wechat/{transport.transport_id}"
            plain = plaintext_headers()
            plain_query = urlencode(
                {
                    "signature": plain["X-Wechat-Signature"],
                    "timestamp": plain["X-Wechat-Timestamp"],
                    "nonce": plain["X-Wechat-Nonce"],
                }
            )
            for body in (MESSAGE, MESSAGE.replace(b"synthetic-message", b"forged-event")):
                with pytest.raises(HTTPError) as denied:
                    urlopen(Request(base + "?" + plain_query, data=body, method="POST"), timeout=5)
                with denied.value as response:
                    assert response.code == 401
                    assert json.loads(response.read()) == {"error": "inbound_unauthorized"}
            for _ in range(2):
                with urlopen(
                    Request(base + "?" + query, data=callback.body, method="POST"), timeout=5
                ) as response:
                    assert response.read() == b"success"
                if frozen is None:
                    frozen = snapshot(service.kernel.database)
                assert snapshot(service.kernel.database) == frozen
            altered = callback.body.replace(b"<Encrypt><![CDATA[", b"<Encrypt><![CDATA[A", 1)
            with pytest.raises(HTTPError) as denied:
                urlopen(Request(base + "?" + query, data=altered, method="POST"), timeout=5)
            with denied.value as response:
                assert response.code == 401
            assert snapshot(service.kernel.database) == frozen
        assert frozen is not None and len(frozen["interactions"]) == 1
    finally:
        service.http.close()
        service.kernel.close()


@pytest.mark.parametrize(
    "kind", ["webhook", "email", "qq", "qq-hmac", "feishu-signed", "feishu-encrypted", "wechat"]
)
def test_signed_callback_body_mutation_cannot_change_the_authenticated_event(kind: str) -> None:
    callback = wire(kind)
    if kind == "wechat":
        body = callback.body.replace(b"<Encrypt><![CDATA[", b"<Encrypt><![CDATA[A", 1)
    elif kind == "feishu-encrypted":
        body = callback.body.replace(b'"encrypt": "', b'"encrypt": "A', 1)
    else:
        body = callback.body.replace(b"synthetic", b"forged")
    assert body != callback.body
    with pytest.raises(InboundAuthenticationError):
        Wire(callback.channel, body, callback.headers, callback.arguments).parse(
            "synthetic-transport"
        )


@pytest.mark.parametrize("kind", ["webhook", "wechat"])
def test_concurrent_authenticated_retries_do_not_duplicate_the_invitation(
    tmp_path: Path, kind: str
) -> None:
    callback = wire(kind)
    kernel, envelope = setup(tmp_path, callback)
    try:

        def ingest(_: int) -> Any:
            return InboundStore(Database(kernel.database.path)).ingest(
                callback.parse(envelope.transport_id)
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ingest, range(4)))
        assert len({result.interaction_id for result in results}) == 1
        assert len({result.event_id for result in results}) == 1
        assert len(snapshot(kernel.database)["interactions"]) == 1
    finally:
        kernel.close()
