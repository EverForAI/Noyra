from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.types import content_hash, utc_now
from noyra.interaction import (
    DeliveryDispatcher,
    InboundEnvelope,
    InboundStore,
    InteractionStore,
    TransportInput,
    TransportStore,
)
from noyra.interaction.outbound import (
    FeishuOutboundAdapter,
    QQOutboundAdapter,
    WeChatOutboundAdapter,
)
from noyra.interaction.transport import _PublicDNSBackend


def test_native_provider_outbound_envelopes() -> None:
    interaction = {"counterparty": "openid-1", "content": "hello", "thread_id": "msg-9"}

    qq = QQOutboundAdapter().build(
        "https://api.sgroup.qq.com",
        {"access_token": "qq-token", "target_type": "user"},
        interaction,
        "idem",
    )
    assert str(qq.endpoint) == "https://api.sgroup.qq.com/v2/users/openid-1/messages"
    assert qq.headers == {"Authorization": "QQBot qq-token"}
    expected_seq = int.from_bytes(hashlib.sha256(b"idem").digest()[:2], "big")
    assert qq.payload == {
        "content": "hello",
        "msg_type": 0,
        "msg_id": "msg-9",
        "msg_seq": expected_seq,
    }
    qq_dm = QQOutboundAdapter().build(
        "https://api.sgroup.qq.com",
        {"access_token": "qq-token", "target_type": "dm"},
        {"counterparty": "guild-1", "content": "hello", "thread_id": "dm-message"},
        "idem-dm",
    )
    assert str(qq_dm.endpoint) == "https://api.sgroup.qq.com/dms/guild-1/messages"
    assert qq_dm.payload["msg_id"] == "dm-message"
    assert "msg_seq" not in qq_dm.payload

    wechat = WeChatOutboundAdapter().build(
        "https://api.weixin.qq.com/cgi-bin/message/custom/send",
        {"access_token": "wx-token"},
        interaction,
        "idem",
    )
    assert "access_token=wx-token" in wechat.endpoint
    assert wechat.payload["touser"] == "openid-1"
    assert wechat.payload["msgtype"] == "text"

    feishu = FeishuOutboundAdapter().build(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        {"tenant_access_token": "lark-token", "receive_id_type": "open_id"},
        interaction,
        "idem",
    )
    assert "receive_id_type=open_id" in feishu.endpoint
    assert feishu.headers == {"Authorization": "Bearer lark-token"}
    assert feishu.payload["receive_id"] == "openid-1"

    custom = FeishuOutboundAdapter().build(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        {"signing_secret": "signing-secret"},
        interaction,
        "idem",
    )
    assert custom.payload["msg_type"] == "text"
    assert custom.payload["content"] == {"text": "hello"}
    timestamp = str(custom.payload["timestamp"])
    expected = base64.b64encode(
        hmac.new(
            f"{timestamp}\nsigning-secret".encode(),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode()
    assert custom.payload["sign"] == expected
    assert "clientmsgid" not in wechat.payload


def test_native_provider_configuration_requires_official_api_hosts() -> None:
    with pytest.raises(ValidationError, match="official API host"):
        TransportInput(
            channel="qq",
            label="qq",
            endpoint="https://example.com",
        )
    with pytest.raises(ValidationError, match="official API host"):
        TransportInput(
            channel="wechat",
            label="wechat",
            endpoint="https://example.com",
        )
    lark = TransportInput(
        channel="feishu",
        label="lark",
        endpoint="https://open.larksuite.com",
    )
    assert lark.endpoint == "https://open.larksuite.com"


def test_qq_recipient_is_quoted_as_one_path_segment() -> None:
    request = QQOutboundAdapter().build(
        "https://api.sgroup.qq.com",
        {"access_token": "qq-token", "target_type": "user"},
        {"counterparty": "user/../../unexpected", "content": "hello"},
        "idem",
    )
    assert str(request.endpoint) == (
        "https://api.sgroup.qq.com/v2/users/user%2F..%2F..%2Funexpected/messages"
    )


@pytest.mark.asyncio
async def test_qq_app_credentials_are_exchanged_and_not_sent_to_provider_api() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-qq-token-test",
            content_hash({"seed": "qq-token"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="qq",
                label="qq app",
                endpoint="https://api.sgroup.qq.com",
                settings={"target_type": "group"},
                credentials={"app_id": SecretStr("app-id"), "app_secret": SecretStr("app-secret")},
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(kernel.subject_id, "qq", "group-openid", "hello")
        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.host == "bots.qq.com":
                assert request.url.path == "/app/getAppAccessToken"
                assert request.content == b'{"appId":"app-id","clientSecret":"app-secret"}'
                return httpx.Response(
                    200, json={"access_token": "short-lived", "expires_in": 7_200}
                )
            assert request.headers["Authorization"] == "QQBot short-lived"
            assert request.url.path == "/v2/groups/group-openid/messages"
            assert "app-secret" not in request.content.decode()
            return httpx.Response(200, json={"id": "qq-message"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            result = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert result[0].status == "delivered"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_qq_reply_uses_authenticated_inbound_transport_route_and_message_id(
    tmp_path: Path,
) -> None:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-qq-native-reply-route",
        content_hash({"seed": "qq-native-reply-route"}),
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    # This transport sorts first and proves a reply is not sent through an
    # unrelated bot account merely because it shares the QQ channel.
    transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="qq",
            label="aaa other account",
            endpoint="https://api.sgroup.qq.com",
            settings={"target_type": "user"},
            credentials={
                "app_id": SecretStr("other-app"),
                "app_secret": SecretStr("other-secret"),
            },
        ),
        actor="operator",
    )
    source = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="qq",
            label="zzz source account",
            endpoint="https://api.sgroup.qq.com",
            # Proactive default deliberately differs from the inbound route.
            settings={"target_type": "user"},
            credentials={
                "app_id": SecretStr("source-app"),
                "app_secret": SecretStr("source-secret"),
            },
        ),
        actor="operator",
    )
    inbound = InboundStore(kernel.database)
    inbound.bind(
        kernel.subject_id,
        source.transport_id,
        external_account_id="source-app",
        external_sender_id="member-openid",
    )
    accepted = inbound.ingest(
        InboundEnvelope(
            channel="qq",
            transport_id=source.transport_id,
            provider_event_id="qq-group-event",
            external_account_id="source-app",
            external_sender_id="member-openid",
            conversation_id="group-openid",
            content="hello",
            thread_id="current-provider-message",
            reply_selector="qq:group",
        )
    )
    inbound.ingest(
        InboundEnvelope(
            channel="qq",
            transport_id=source.transport_id,
            provider_event_id="qq-newer-group-event",
            external_account_id="source-app",
            external_sender_id="member-openid",
            conversation_id="group-openid",
            content="a newer unrelated message",
            thread_id="newer-provider-message",
            reply_selector="qq:group",
        )
    )
    reply = InteractionStore(kernel.database).send(
        kernel.subject_id,
        "qq",
        "group-openid",
        "native reply",
        related_interaction_id=accepted.interaction_id,
        idempotency_key="qq-native-reply",
    )
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "bots.qq.com":
            assert request.content == (b'{"appId":"source-app","clientSecret":"source-secret"}')
            return httpx.Response(200, json={"access_token": "source-token", "expires_in": 7_200})
        assert request.url.path == "/v2/groups/group-openid/messages"
        assert request.headers["Authorization"] == "QQBot source-token"
        assert json.loads(request.content) == {
            "content": "native reply",
            "msg_type": 0,
            "msg_id": "current-provider-message",
            "msg_seq": int.from_bytes(
                hashlib.sha256(
                    f"interaction:{reply.interaction_id}:transport:{source.transport_id}".encode()
                ).digest()[:2],
                "big",
            ),
        }
        return httpx.Response(200, json={"id": "qq-reply"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
    try:
        result = await dispatcher.deliver_pending(kernel.subject_id)
    finally:
        await client.aclose()
    assert result[0].status == "delivered"
    assert result[0].transport_id == source.transport_id
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_qq_token_is_refreshed_once_before_delivery() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-qq-token-refresh",
            content_hash({"seed": "qq-token-refresh"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="qq",
                label="qq app",
                endpoint="https://api.sgroup.qq.com",
                settings={"target_type": "user"},
                credentials={"app_id": SecretStr("app-id"), "app_secret": SecretStr("app-secret")},
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(kernel.subject_id, "qq", "user-openid", "hello")
        token_calls = 0
        send_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls, send_calls
            if request.url.host == "bots.qq.com":
                token_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "stale-token" if token_calls == 1 else "fresh-token",
                        "expires_in": 7200,
                    },
                )
            send_calls += 1
            if send_calls == 1:
                return httpx.Response(401, json={"code": 401, "message": "access token expired"})
            assert request.headers["Authorization"] == "QQBot fresh-token"
            return httpx.Response(200, json={"id": "qq-message"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            result = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert result[0].status == "delivered"
        assert token_calls == 2
        assert send_calls == 2


@pytest.mark.asyncio
async def test_feishu_app_credentials_use_native_token_and_receive_id_selector() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-feishu-token-test",
            content_hash({"seed": "feishu-token"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="feishu app",
                endpoint="https://open.feishu.cn",
                settings={"receive_id_type": "email"},
                credentials={"app_id": SecretStr("app-id"), "app_secret": SecretStr("app-secret")},
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id, "feishu", "person@example.com", "hello"
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                assert request.content == b'{"app_id":"app-id","app_secret":"app-secret"}'
                return httpx.Response(
                    200, json={"tenant_access_token": "tenant-token", "expire": 7_200}
                )
            assert request.url.path == "/open-apis/im/v1/messages"
            assert request.url.params["receive_id_type"] == "email"
            assert request.headers["Authorization"] == "Bearer tenant-token"
            assert "app-secret" not in request.content.decode()
            return httpx.Response(200, json={"data": {"message_id": "lark-message"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            result = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert result[0].status == "delivered"


@pytest.mark.asyncio
async def test_feishu_inbound_reply_overrides_proactive_selector_with_chat_id(
    tmp_path: Path,
) -> None:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-feishu-native-reply-route",
        content_hash({"seed": "feishu-native-reply-route"}),
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    transport = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="feishu",
            label="feishu app",
            endpoint="https://open.feishu.cn",
            settings={"receive_id_type": "open_id", "delivery_mode": "app"},
            credentials={
                "app_id": SecretStr("cli-app"),
                "app_secret": SecretStr("app-secret"),
            },
        ),
        actor="operator",
    )
    inbound = InboundStore(kernel.database)
    inbound.bind(
        kernel.subject_id,
        transport.transport_id,
        external_account_id="cli-app",
        external_sender_id="ou-user",
    )
    accepted = inbound.ingest(
        InboundEnvelope(
            channel="feishu",
            transport_id=transport.transport_id,
            provider_event_id="feishu-event",
            external_account_id="cli-app",
            external_sender_id="ou-user",
            conversation_id="oc-chat",
            content="hello",
            reply_selector="feishu:chat_id",
        )
    )
    InteractionStore(kernel.database).send(
        kernel.subject_id,
        "feishu",
        "oc-chat",
        "native reply",
        related_interaction_id=accepted.interaction_id,
        idempotency_key="feishu-native-reply",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"tenant_access_token": "tenant-token", "expire": 7_200}
            )
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.url.params["receive_id_type"] == "chat_id"
        assert json.loads(request.content)["receive_id"] == "oc-chat"
        return httpx.Response(200, json={"data": {"message_id": "feishu-reply"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
    try:
        result = await dispatcher.deliver_pending(kernel.subject_id)
    finally:
        await client.aclose()
    assert result[0].status == "delivered"


@pytest.mark.asyncio
async def test_wechat_app_credentials_use_native_token_and_customer_message_shape() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-wechat-token-test",
            content_hash({"seed": "wechat-token"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="wechat",
                label="wechat app",
                endpoint="https://api.weixin.qq.com",
                credentials={"app_id": SecretStr("app-id"), "app_secret": SecretStr("app-secret")},
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(kernel.subject_id, "wechat", "wx-user", "hello")

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/cgi-bin/token":
                assert request.url.params["appid"] == "app-id"
                assert request.url.params["secret"] == "app-secret"
                return httpx.Response(
                    200, json={"access_token": "wechat-token", "expires_in": 7_200}
                )
            assert request.url.path == "/cgi-bin/message/custom/send"
            assert request.url.params["access_token"] == "wechat-token"
            assert request.content == (
                b'{"touser":"wx-user","msgtype":"text","text":{"content":"hello"}}'
            )
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            result = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert result[0].status == "delivered"


def test_transport_integrity_rejects_blob_config_json() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-integrity",
            content_hash({"seed": "transport-integrity"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transport = transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="webhook",
                label="integrity",
                endpoint="https://example.com/webhook",
                credentials={"token": SecretStr("integrity-secret")},
            ),
            actor="operator",
        )
        with kernel.database.connection() as connection:
            config_json = connection.execute(
                "SELECT config_json FROM interaction_transports WHERE transport_id = ?",
                (transport.transport_id,),
            ).fetchone()[0]
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE interaction_transports SET config_json = ? WHERE transport_id = ?",
                (sqlite3.Binary(config_json.encode("utf-8")), transport.transport_id),
            )
        with pytest.raises(IntegrityError):
            transports.verify_integrity(kernel.subject_id)


def test_transport_integrity_rejects_secret_endpoint_tampering() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-endpoint-integrity",
            content_hash({"seed": "transport-endpoint-integrity"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transport = transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="hook",
                endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/real-key",
            ),
            actor="operator",
        )
        secret_path = root / "secrets" / f"{transport.transport_id}.json"
        payload = json.loads(secret_path.read_text())
        payload["endpoint"] = "https://evil.example/open-apis/bot/v2/hook/other-key"
        secret_path.write_text(json.dumps(payload))
        with pytest.raises(IntegrityError, match="endpoint"):
            transports.verify_integrity(kernel.subject_id)


def test_transport_integrity_rejects_same_origin_secret_path_tampering() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-endpoint-path-integrity",
            content_hash({"seed": "transport-endpoint-path-integrity"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transport = transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="hook",
                endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/real-key",
            ),
            actor="operator",
        )
        secret_path = root / "secrets" / f"{transport.transport_id}.json"
        payload = json.loads(secret_path.read_text())
        payload["endpoint"] = "https://open.feishu.cn/open-apis/bot/v2/hook/other-key?x=1"
        secret_path.write_text(json.dumps(payload))
        with pytest.raises(IntegrityError, match="endpoint"):
            transports.verify_integrity(kernel.subject_id)


def test_transport_integrity_rejects_same_endpoint_secret_reference_swap(
    tmp_path: Path,
) -> None:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-transport-secret-reference-integrity",
        content_hash({"seed": "transport-secret-reference-integrity"}),
    )
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    first = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="telegram",
            label="first",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("first-token")},
        ),
        actor="operator",
    )
    second = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="telegram",
            label="second",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("second-token")},
        ),
        actor="operator",
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interaction_transports SET secret_reference = CASE transport_id "
            "WHEN ? THEN ? WHEN ? THEN ? END WHERE transport_id IN (?, ?)",
            (
                first.transport_id,
                f"{second.transport_id}.json",
                second.transport_id,
                f"{first.transport_id}.json",
                first.transport_id,
                second.transport_id,
            ),
        )

    with pytest.raises(IntegrityError, match="state hash"):
        transports.verify_integrity(kernel.subject_id)
    with pytest.raises(IntegrityError, match="secret reference"):
        transports.secret(first.transport_id, subject_id=kernel.subject_id)


def _downgrade_transport_to_legacy_endpoint(
    kernel: SubjectKernel,
    secret_dir: Path,
    transport_id: str,
    *,
    private_endpoint: str,
    durable_origin: str,
    status: str,
) -> None:
    secret_path = secret_dir / f"{transport_id}.json"
    payload = json.loads(secret_path.read_text("utf-8"))
    payload["endpoint"] = private_endpoint
    secret_path.write_text(json.dumps(payload), "utf-8")
    with kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM interaction_transports WHERE transport_id = ?",
            (transport_id,),
        ).fetchone()
        legacy_hash = content_hash(
            {
                "transport_id": transport_id,
                "subject_id": row["subject_id"],
                "channel": row["channel"],
                "label": row["label"],
                "endpoint": durable_origin,
                "config": json.loads(row["config_json"]),
                "status": status,
            }
        )
        connection.execute(
            "UPDATE interaction_transports SET endpoint = ?, "
            "endpoint_contract = 'legacy_origin', endpoint_digest = NULL, "
            "status = ?, state_hash = ? WHERE transport_id = ?",
            (durable_origin, status, legacy_hash, transport_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ("active", "disabled"))
@pytest.mark.parametrize(
    ("case", "private_endpoint", "durable_origin", "sensitive_marker"),
    (
        (
            "off-host",
            "https://legacy.invalid/bot/private-off-host?token=do-not-log",
            "https://legacy.invalid",
            "private-off-host",
        ),
        (
            "userinfo-fragment",
            "https://do-not-log-user:do-not-log-password@api.telegram.org/"
            "bot/private-userinfo#private-fragment",
            "https://api.telegram.org",
            "private-userinfo",
        ),
    ),
)
async def test_legacy_policy_incompatible_endpoint_is_revoked_and_recoverable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    initial_status: str,
    case: str,
    private_endpoint: str,
    durable_origin: str,
    sensitive_marker: str,
) -> None:
    subject_id = f"Noyra-legacy-policy-{case}-{initial_status}"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    secret_dir = tmp_path / "secrets" / "transports"
    transports = TransportStore(kernel.database, secret_dir)
    legacy = transports.configure(
        subject_id,
        TransportInput(
            channel="telegram",
            label="a legacy",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("legacy-token")},
        ),
        actor="operator",
    )
    current = transports.configure(
        subject_id,
        TransportInput(
            channel="telegram",
            label="z current",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("current-token")},
        ),
        actor="operator",
    )
    pending = InteractionStore(kernel.database).send(
        subject_id,
        "telegram",
        "123456",
        "must not use the retired legacy credential",
        idempotency_key=f"legacy-policy-{case}-{initial_status}",
    )
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    assert dispatcher.enqueue_pending(subject_id) == 1
    with kernel.database.connection() as connection:
        queued_transport_id = connection.execute(
            "SELECT transport_id FROM interaction_deliveries WHERE interaction_id = ?",
            (pending.interaction_id,),
        ).fetchone()[0]
    assert queued_transport_id == legacy.transport_id

    _downgrade_transport_to_legacy_endpoint(
        kernel,
        secret_dir,
        legacy.transport_id,
        private_endpoint=private_endpoint,
        durable_origin=durable_origin,
        status=initial_status,
    )
    audit = IntegrityRegistry().run(
        kernel.database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("interaction.transport",),
    )
    assert audit.status == "ok"

    caplog.set_level("WARNING", logger="noyra.interaction.transport")
    deferred = TransportStore(kernel.database, secret_dir, repair_on_init=False)
    assert deferred._upgrade_legacy_endpoints(subject_id) == 1
    migrated = deferred.get(legacy.transport_id, subject_id=subject_id)
    assert migrated.status == "revoked"
    assert migrated.endpoint == durable_origin
    assert not (secret_dir / f"{legacy.transport_id}.json").exists()
    assert deferred.get(current.transport_id, subject_id=subject_id).status == "active"
    assert deferred.secret(current.transport_id, subject_id=subject_id)["credentials"] == {
        "bot_token": "current-token"
    }
    with kernel.database.connection() as connection:
        durable = connection.execute(
            "SELECT endpoint, endpoint_contract, endpoint_digest, secret_reference, status, "
            "state_hash FROM interaction_transports WHERE transport_id = ?",
            (legacy.transport_id,),
        ).fetchone()
        delete_intent = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_type = 'transport' "
            "AND resource_id = ? AND operation = 'delete'",
            (legacy.transport_id,),
        ).fetchone()
    assert durable["endpoint_contract"] == "digest_v1"
    assert len(durable["endpoint_digest"]) == 64
    assert durable["secret_reference"] == f"{legacy.transport_id}.json"
    assert durable["status"] == "revoked"
    assert sensitive_marker not in "".join(str(value) for value in durable)
    assert delete_intent["state"] == "removed"
    warning = "\n".join(caplog.messages)
    assert legacy.transport_id in warning
    assert "telegram" in warning
    assert "reconfigure" in warning
    assert sensitive_marker not in warning
    assert "do-not-log-password" not in warning
    with pytest.raises(PermissionError, match="unavailable"):
        deferred.secret(legacy.transport_id, subject_id=subject_id)
    with pytest.raises(ValueError, match="revoked"):
        deferred.enable(
            legacy.transport_id,
            reason="migration cannot be bypassed",
            actor="operator",
            subject_id=subject_id,
        )

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message_id": "current-provider-message"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    migrated_dispatcher = DeliveryDispatcher(kernel.database, deferred, client=client)
    try:
        rejected = await migrated_dispatcher.deliver_pending(subject_id)
        assert len(rejected) == 1
        assert rejected[0].status == "failed"
        assert rejected[0].last_error == "PermissionError"
        assert requests == []

        InteractionStore(kernel.database).send(
            subject_id,
            "telegram",
            "123456",
            "current transport remains operational",
            idempotency_key=f"current-policy-{case}-{initial_status}",
        )
        delivered = await migrated_dispatcher.deliver_pending(subject_id)
    finally:
        await client.aclose()
    assert len(delivered) == 1
    assert delivered[0].status == "delivered"
    assert len(requests) == 1


def test_service_boot_recovers_legacy_policy_incompatible_transport(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from noyra.service import NoyraService, ServiceSettings

    data_dir = tmp_path / "service"
    subject_id = "Noyra-legacy-policy-service-recovery"
    settings = ServiceSettings(
        data_dir=data_dir,
        subject_id=subject_id,
        genesis_hash=content_hash({"subject": subject_id}),
        host="127.0.0.1",
        port=0,
    )
    seed = NoyraService(settings)
    legacy_id = current_id = ""
    try:
        seed.boot()
        legacy = seed.http.transports.configure(
            subject_id,
            TransportInput(
                channel="telegram",
                label="legacy",
                endpoint="https://api.telegram.org",
                credentials={"bot_token": SecretStr("legacy-token")},
            ),
            actor="operator",
        )
        current = seed.http.transports.configure(
            subject_id,
            TransportInput(
                channel="telegram",
                label="current",
                endpoint="https://api.telegram.org",
                credentials={"bot_token": SecretStr("current-token")},
            ),
            actor="operator",
        )
        legacy_id = legacy.transport_id
        current_id = current.transport_id
        _downgrade_transport_to_legacy_endpoint(
            seed.kernel,
            seed.http.transports.secret_dir,
            legacy_id,
            private_endpoint="https://legacy.invalid/private-service-path?token=must-not-log",
            durable_origin="https://legacy.invalid",
            status="active",
        )
    finally:
        seed.close()

    caplog.set_level("WARNING", logger="noyra.interaction.transport")
    service = NoyraService(settings)
    try:
        service.boot()
        assert service.kernel.lifecycle.current().state == "active"
        assert service.http.transports.get(legacy_id, subject_id=subject_id).status == "revoked"
        assert service.http.transports.get(current_id, subject_id=subject_id).status == "active"
        assert not (service.http.transports.secret_dir / f"{legacy_id}.json").exists()
        assert (service.http.transports.secret_dir / f"{current_id}.json").exists()
    finally:
        service.close()
    warning = "\n".join(caplog.messages)
    assert legacy_id in warning
    assert "telegram" in warning
    assert "reconfigure" in warning
    assert "private-service-path" not in warning
    assert "must-not-log" not in warning


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (("state_hash", "state hash"), ("reference", "reference"), ("origin", "origin mismatch")),
)
def test_legacy_policy_incompatible_endpoint_does_not_mask_corruption(
    tmp_path: Path,
    corruption: str,
    expected_error: str,
) -> None:
    subject_id = f"Noyra-legacy-corruption-{corruption}"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    secret_dir = tmp_path / "secrets" / "transports"
    transports = TransportStore(kernel.database, secret_dir)
    record = transports.configure(
        subject_id,
        TransportInput(
            channel="telegram",
            label="legacy",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("legacy-token")},
        ),
        actor="operator",
    )
    private_endpoint = "https://legacy.invalid/private-path?token=private-query"
    _downgrade_transport_to_legacy_endpoint(
        kernel,
        secret_dir,
        record.transport_id,
        private_endpoint=private_endpoint,
        durable_origin="https://legacy.invalid",
        status="active",
    )
    with kernel.database.transaction() as connection:
        if corruption == "state_hash":
            connection.execute(
                "UPDATE interaction_transports SET state_hash = ? WHERE transport_id = ?",
                ("0" * 64, record.transport_id),
            )
        elif corruption == "reference":
            connection.execute(
                "UPDATE interaction_transports SET secret_reference = ? WHERE transport_id = ?",
                ("transport_other.json", record.transport_id),
            )
        else:
            mismatched_origin = "https://api.telegram.org"
            mismatched_hash = content_hash(
                {
                    "transport_id": record.transport_id,
                    "subject_id": subject_id,
                    "channel": "telegram",
                    "label": "legacy",
                    "endpoint": mismatched_origin,
                    "config": {},
                    "status": "active",
                }
            )
            connection.execute(
                "UPDATE interaction_transports SET endpoint = ?, state_hash = ? "
                "WHERE transport_id = ?",
                (mismatched_origin, mismatched_hash, record.transport_id),
            )

    deferred = TransportStore(kernel.database, secret_dir, repair_on_init=False)
    with pytest.raises(IntegrityError, match=expected_error):
        deferred._upgrade_legacy_endpoints(subject_id)
    with kernel.database.connection() as connection:
        durable = connection.execute(
            "SELECT endpoint_contract, status FROM interaction_transports WHERE transport_id = ?",
            (record.transport_id,),
        ).fetchone()
        delete_intent = connection.execute(
            "SELECT 1 FROM secret_file_intents WHERE resource_type = 'transport' "
            "AND resource_id = ? AND operation = 'delete'",
            (record.transport_id,),
        ).fetchone()
    assert durable["endpoint_contract"] == "legacy_origin"
    assert durable["status"] == "active"
    assert delete_intent is None
    assert (secret_dir / f"{record.transport_id}.json").exists()


def test_legacy_policy_revocation_rechecks_the_locked_transport_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-legacy-locked-row"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    secret_dir = tmp_path / "secrets" / "transports"
    transports = TransportStore(kernel.database, secret_dir)
    record = transports.configure(
        subject_id,
        TransportInput(
            channel="telegram",
            label="legacy",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("legacy-token")},
        ),
        actor="operator",
    )
    _downgrade_transport_to_legacy_endpoint(
        kernel,
        secret_dir,
        record.transport_id,
        private_endpoint="https://legacy.invalid/private-path?token=private-query",
        durable_origin="https://legacy.invalid",
        status="active",
    )
    original_transaction = kernel.database.transaction
    injected = False

    @contextmanager
    def transaction_with_concurrent_change() -> Iterator[sqlite3.Connection]:
        nonlocal injected
        if not injected:
            injected = True
            with original_transaction() as connection:
                connection.execute(
                    "UPDATE interaction_transports SET label = ? WHERE transport_id = ?",
                    ("concurrently changed", record.transport_id),
                )
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(kernel.database, "transaction", transaction_with_concurrent_change)
    deferred = TransportStore(kernel.database, secret_dir, repair_on_init=False)
    with pytest.raises(IntegrityError, match="changed during endpoint upgrade"):
        deferred._upgrade_legacy_endpoints(subject_id)
    with kernel.database.connection() as connection:
        durable = connection.execute(
            "SELECT endpoint_contract, status FROM interaction_transports WHERE transport_id = ?",
            (record.transport_id,),
        ).fetchone()
        delete_intent = connection.execute(
            "SELECT 1 FROM secret_file_intents WHERE resource_type = 'transport' "
            "AND resource_id = ? AND operation = 'delete'",
            (record.transport_id,),
        ).fetchone()
    assert durable["endpoint_contract"] == "legacy_origin"
    assert durable["status"] == "active"
    assert delete_intent is None
    assert (secret_dir / f"{record.transport_id}.json").exists()


def test_legacy_policy_revocation_retries_secret_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "Noyra-legacy-cleanup-retry"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    secret_dir = tmp_path / "secrets" / "transports"
    transports = TransportStore(kernel.database, secret_dir)
    record = transports.configure(
        subject_id,
        TransportInput(
            channel="telegram",
            label="legacy",
            endpoint="https://api.telegram.org",
            credentials={"bot_token": SecretStr("legacy-token")},
        ),
        actor="operator",
    )
    _downgrade_transport_to_legacy_endpoint(
        kernel,
        secret_dir,
        record.transport_id,
        private_endpoint="https://legacy.invalid/private-path?token=private-query",
        durable_origin="https://legacy.invalid",
        status="active",
    )
    secret_path = secret_dir / f"{record.transport_id}.json"
    original_unlink = Path.unlink
    failed_once = False

    def fail_first_secret_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal failed_once
        if path == secret_path and not failed_once:
            failed_once = True
            raise OSError("injected legacy secret deletion failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_first_secret_unlink)
    deferred = TransportStore(kernel.database, secret_dir, repair_on_init=False)
    assert deferred._upgrade_legacy_endpoints(subject_id) == 1
    assert deferred.get(record.transport_id, subject_id=subject_id).status == "revoked"
    assert secret_path.exists()
    assert deferred.secret_cleanup.health(subject_id, "transport")["status"] == "degraded"
    with kernel.database.connection() as connection:
        intent = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_type = 'transport' "
            "AND resource_id = ? AND operation = 'delete'",
            (record.transport_id,),
        ).fetchone()
    assert intent["state"] == "pending"

    assert deferred.secret_cleanup.repair(subject_id, "transport", secret_dir) == 1
    assert not secret_path.exists()
    assert deferred.secret_cleanup.health(subject_id, "transport")["status"] == "ok"
    with kernel.database.connection() as connection:
        repaired_intent = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_type = 'transport' "
            "AND resource_id = ? AND operation = 'delete'",
            (record.transport_id,),
        ).fetchone()
    assert repaired_intent["state"] == "removed"


def test_schema_49_legacy_endpoint_is_upgraded_to_private_endpoint_digest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "noyra.sqlite3"
    subject_id = "Noyra-transport-endpoint-upgrade"
    kernel = SubjectKernel(
        database_path,
        subject_id,
        content_hash({"seed": "transport-endpoint-upgrade"}),
    )
    secret_dir = tmp_path / "secrets"
    store = TransportStore(kernel.database, secret_dir)
    record = store.configure(
        subject_id,
        TransportInput(
            channel="feishu",
            label="legacy hook",
            endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/private-key?mode=1",
            credentials={"signing_secret": SecretStr("signing-secret")},
        ),
        actor="operator",
    )
    legacy_endpoint = "https://open.feishu.cn"
    legacy_hash = content_hash(
        {
            "transport_id": record.transport_id,
            "subject_id": subject_id,
            "channel": "feishu",
            "label": "legacy hook",
            "endpoint": legacy_endpoint,
            "config": {},
            "status": "active",
        }
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interaction_transports SET endpoint = ?, state_hash = ? WHERE transport_id = ?",
            (legacy_endpoint, legacy_hash, record.transport_id),
        )
        connection.execute("DROP TRIGGER prevent_interaction_inbound_identity_update")
        connection.execute("DROP INDEX idx_interaction_inbound_interaction")
        connection.execute(
            "ALTER TABLE interaction_inbound_events DROP COLUMN reply_context_version"
        )
        connection.execute("ALTER TABLE interaction_inbound_events DROP COLUMN reply_selector")
        connection.execute("ALTER TABLE interaction_inbound_events DROP COLUMN external_thread_id")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_digest")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_contract")
        connection.execute("DROP TRIGGER validate_public_post_moderation_transition")
        connection.execute("DROP TRIGGER validate_public_post_identity_insert")
        connection.execute("DROP TRIGGER prevent_public_post_identity_update")
        connection.execute("DROP INDEX uq_public_post_moderation_revision")
        connection.execute("DROP INDEX uq_public_post_moderation_idempotency")
        connection.execute("DROP INDEX idx_public_post_rate_events_subject_time")
        connection.execute("DROP INDEX idx_public_post_captcha_issue_subject_time")
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN idempotency_key")
        connection.execute(
            "ALTER TABLE public_post_moderation_events DROP COLUMN previous_event_id"
        )
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN revision")
        connection.execute("ALTER TABLE public_posts DROP COLUMN author_provenance")
        connection.execute("ALTER TABLE public_posts DROP COLUMN identity_hash")
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_global_rate_per_minute"
        )
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_issue_limit_per_hour"
        )
        connection.execute("ALTER TABLE public_post_controls DROP COLUMN storage_cap_bytes")
        connection.execute("UPDATE schema_meta SET value = '49' WHERE key = 'schema_version'")
    kernel.close()

    upgraded_database = Database(database_path)
    upgraded_store = TransportStore(upgraded_database, secret_dir)
    upgraded = upgraded_store.get(record.transport_id, subject_id=subject_id)
    assert upgraded.endpoint == "https://open.feishu.cn"
    assert upgraded_store.secret(record.transport_id, subject_id=subject_id)["endpoint"] == (
        "https://open.feishu.cn/open-apis/bot/v2/hook/private-key?mode=1"
    )
    with upgraded_database.connection() as connection:
        durable = connection.execute(
            "SELECT endpoint, endpoint_contract, endpoint_digest "
            "FROM interaction_transports WHERE transport_id = ?",
            (record.transport_id,),
        ).fetchone()
        schema = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert durable["endpoint"] == "https://open.feishu.cn"
    assert durable["endpoint_contract"] == "digest_v1"
    assert len(durable["endpoint_digest"]) == 64
    assert "private-key" not in "".join(str(value) for value in durable)
    assert schema["value"] == str(CURRENT_SCHEMA_VERSION)


@pytest.mark.asyncio
async def test_schema_50_reply_context_upgrade_preserves_legacy_queued_reply(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "noyra.sqlite3"
    subject_id = "Noyra-schema-50-reply-context"
    kernel = SubjectKernel(
        database_path,
        subject_id,
        content_hash({"seed": "schema-50-reply-context"}),
    )
    secret_dir = tmp_path / "secrets"
    transports = TransportStore(kernel.database, secret_dir)
    source = transports.configure(
        subject_id,
        TransportInput(
            channel="qq",
            label="legacy qq",
            endpoint="https://api.sgroup.qq.com",
            settings={"target_type": "user"},
            credentials={"access_token": SecretStr("must-not-be-used")},
        ),
        actor="operator",
    )
    inbound = InboundStore(kernel.database)
    inbound.bind(
        subject_id,
        source.transport_id,
        external_account_id="legacy-app",
        external_sender_id="legacy-member",
    )
    accepted = inbound.ingest(
        InboundEnvelope(
            channel="qq",
            transport_id=source.transport_id,
            provider_event_id="legacy-group-event",
            external_account_id="legacy-app",
            external_sender_id="legacy-member",
            conversation_id="legacy-group",
            content="historical inbound",
            thread_id="legacy-provider-message",
            reply_selector="qq:group",
        )
    )
    reply = InteractionStore(kernel.database).send(
        subject_id,
        "qq",
        "legacy-group",
        "historical queued reply",
        related_interaction_id=accepted.interaction_id,
        idempotency_key="schema-50-queued-reply",
    )
    dispatcher = DeliveryDispatcher(kernel.database, transports)
    assert dispatcher.enqueue_pending(subject_id) == 1
    with kernel.database.connection() as connection:
        queued = connection.execute(
            "SELECT delivery_id FROM interaction_deliveries WHERE interaction_id = ?",
            (reply.interaction_id,),
        ).fetchone()
    delivery_id = str(queued["delivery_id"])

    with kernel.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_interaction_inbound_identity_update")
        connection.execute("DROP INDEX idx_interaction_inbound_interaction")
        connection.execute(
            "ALTER TABLE interaction_inbound_events DROP COLUMN reply_context_version"
        )
        connection.execute("ALTER TABLE interaction_inbound_events DROP COLUMN reply_selector")
        connection.execute("ALTER TABLE interaction_inbound_events DROP COLUMN external_thread_id")
        connection.execute("DROP TRIGGER validate_public_post_moderation_transition")
        connection.execute("DROP TRIGGER validate_public_post_identity_insert")
        connection.execute("DROP TRIGGER prevent_public_post_identity_update")
        connection.execute("DROP INDEX uq_public_post_moderation_revision")
        connection.execute("DROP INDEX uq_public_post_moderation_idempotency")
        connection.execute("DROP INDEX idx_public_post_rate_events_subject_time")
        connection.execute("DROP INDEX idx_public_post_captcha_issue_subject_time")
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN idempotency_key")
        connection.execute(
            "ALTER TABLE public_post_moderation_events DROP COLUMN previous_event_id"
        )
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN revision")
        connection.execute("ALTER TABLE public_posts DROP COLUMN author_provenance")
        connection.execute("ALTER TABLE public_posts DROP COLUMN identity_hash")
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_global_rate_per_minute"
        )
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_issue_limit_per_hour"
        )
        connection.execute("ALTER TABLE public_post_controls DROP COLUMN storage_cap_bytes")
        connection.execute("UPDATE schema_meta SET value = '50' WHERE key = 'schema_version'")
    kernel.close()

    upgraded_database = Database(database_path)
    upgraded_store = TransportStore(upgraded_database, secret_dir)
    with upgraded_database.connection() as connection:
        migrated_event = connection.execute(
            "SELECT external_thread_id, reply_selector, reply_context_version, "
            "interaction_id FROM interaction_inbound_events "
            "WHERE provider_event_id = 'legacy-group-event'"
        ).fetchone()
        migrated_delivery = connection.execute(
            "SELECT delivery_id, interaction_id, transport_id, status, attempts "
            "FROM interaction_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        schema = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert schema["value"] == str(CURRENT_SCHEMA_VERSION)
    assert migrated_event["interaction_id"] == accepted.interaction_id
    assert migrated_event["external_thread_id"] is None
    assert migrated_event["reply_selector"] is None
    assert migrated_event["reply_context_version"] == 0
    assert migrated_delivery["delivery_id"] == delivery_id
    assert migrated_delivery["interaction_id"] == reply.interaction_id
    assert migrated_delivery["transport_id"] == source.transport_id
    assert migrated_delivery["status"] == "queued"
    assert migrated_delivery["attempts"] == 0

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "must-not-send"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    migrated_dispatcher = DeliveryDispatcher(upgraded_database, upgraded_store, client=client)
    try:
        result = await migrated_dispatcher.deliver_pending(subject_id)
    finally:
        await client.aclose()
    assert len(result) == 1
    assert result[0].delivery_id == delivery_id
    assert result[0].status == "failed"
    assert result[0].last_error == "IntegrityError"
    assert result[0].attempts == 1
    assert requests == []


def test_legacy_endpoint_upgrade_is_scoped_to_the_active_subject(tmp_path: Path) -> None:
    subject_a = "Noyra-endpoint-upgrade-a"
    subject_b = "Noyra-endpoint-upgrade-b"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_a,
        content_hash({"seed": subject_a}),
    )
    IdentityStore(kernel.database).ensure(subject_b, content_hash({"seed": subject_b}))
    secret_dir = tmp_path / "secrets"
    store = TransportStore(kernel.database, secret_dir)
    records = {
        subject: store.configure(
            subject,
            TransportInput(
                channel="feishu",
                label=f"hook {subject[-1]}",
                endpoint=f"https://open.feishu.cn/open-apis/bot/v2/hook/key-{subject[-1]}",
            ),
            actor="operator",
        )
        for subject in (subject_a, subject_b)
    }
    with kernel.database.transaction() as connection:
        for subject, record in records.items():
            legacy_endpoint = "https://open.feishu.cn"
            legacy_hash = content_hash(
                {
                    "transport_id": record.transport_id,
                    "subject_id": subject,
                    "channel": "feishu",
                    "label": f"hook {subject[-1]}",
                    "endpoint": legacy_endpoint,
                    "config": {},
                    "status": "active",
                }
            )
            connection.execute(
                "UPDATE interaction_transports SET endpoint = ?, "
                "endpoint_contract = 'legacy_origin', endpoint_digest = NULL, state_hash = ? "
                "WHERE transport_id = ?",
                (legacy_endpoint, legacy_hash, record.transport_id),
            )
    # A damaged foreign subject must neither be repaired nor block A's
    # post-integrity recovery boundary.
    (secret_dir / f"{records[subject_b].transport_id}.json").unlink()
    deferred = TransportStore(kernel.database, secret_dir, repair_on_init=False)
    assert deferred._upgrade_legacy_endpoints(subject_a) == 1
    with kernel.database.connection() as connection:
        rows = {
            row["subject_id"]: row
            for row in connection.execute(
                "SELECT subject_id, endpoint_contract, endpoint_digest "
                "FROM interaction_transports WHERE subject_id IN (?, ?)",
                (subject_a, subject_b),
            ).fetchall()
        }
    assert rows[subject_a]["endpoint_contract"] == "digest_v1"
    assert len(rows[subject_a]["endpoint_digest"]) == 64
    assert rows[subject_b]["endpoint_contract"] == "legacy_origin"
    assert rows[subject_b]["endpoint_digest"] is None


@pytest.mark.asyncio
async def test_telegram_delivery_is_idempotent_and_tracks_provider_outcome() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-test",
            content_hash({"seed": "transport"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transport = transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="telegram",
                label="primary",
                endpoint="https://api.telegram.org",
                credentials={"bot_token": SecretStr("test-bot-token")},
            ),
            actor="operator",
        )
        interaction = InteractionStore(kernel.database).send(
            kernel.subject_id,
            "telegram",
            "123456",
            "A voluntary message from the subject.",
            idempotency_key="telegram-message",
        )
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"message_id": "provider-42"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            first = await dispatcher.deliver_pending(kernel.subject_id)
            second = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert len(first) == 1
        assert first[0].status == "delivered"
        assert first[0].provider_message_id == "provider-42"
        assert second == []
        assert len(requests) == 1
        assert interaction.status == "sent"
        assert transport.endpoint == "https://api.telegram.org"
        with kernel.database.connection() as connection:
            persisted = connection.execute(
                "SELECT endpoint, config_json FROM interaction_transports WHERE transport_id = ?",
                (transport.transport_id,),
            ).fetchone()
        assert "test-bot-token" not in persisted["endpoint"] + persisted["config_json"]


@pytest.mark.asyncio
async def test_cross_subject_delivery_is_failed_closed_before_external_send() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subject_a = "Noyra-transport-owner-a"
        subject_b = "Noyra-transport-owner-b"
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            subject_a,
            content_hash({"seed": subject_a}),
        )
        IdentityStore(kernel.database).ensure(subject_b, content_hash({"seed": subject_b}))
        transports = TransportStore(kernel.database, root / "secrets")
        foreign_transport = transports.configure(
            subject_b,
            TransportInput(
                channel="webhook",
                label="foreign",
                endpoint="https://foreign.example/hook",
                credentials={"token": SecretStr("foreign-secret")},
            ),
            actor="operator",
        )
        interaction = InteractionStore(kernel.database).send(
            subject_a,
            "webhook",
            "recipient-a",
            "This must never cross the subject boundary.",
            idempotency_key="cross-subject-delivery",
        )
        now = utc_now()
        with kernel.database.transaction() as connection:
            # The production schema rejects this at INSERT time.  Drop only
            # the generated guard in this isolated corruption fixture so the
            # dispatcher-level fail-closed check is exercised as well.
            connection.execute(
                "DROP TRIGGER validate_interaction_deliveries_transport_id_subject_insert"
            )
            connection.execute(
                "INSERT INTO interaction_deliveries(delivery_id, interaction_id, subject_id, "
                "transport_id, idempotency_key, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                (
                    "delivery-cross-subject",
                    interaction.interaction_id,
                    subject_a,
                    foreign_transport.transport_id,
                    "cross-subject-delivery",
                    now,
                    now,
                ),
            )
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"message_id": "must-not-send"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            result = await dispatcher.deliver_pending(subject_a)
        finally:
            await client.aclose()

        assert len(result) == 1
        assert result[0].status == "failed"
        assert result[0].last_error == "delivery ownership mismatch"
        assert result[0].attempts >= 3
        assert requests == []


@pytest.mark.asyncio
async def test_ambiguous_transport_result_is_not_retried_automatically() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-unknown",
            content_hash({"seed": "transport-unknown"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="primary",
                endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id,
            "feishu",
            "team",
            "Please review this bounded request.",
        )

        async def ambiguous(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("response lost", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(ambiguous))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            delivered = await dispatcher.deliver_pending(kernel.subject_id)
            retried = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert delivered[0].status == "unknown"
        assert retried == []


@pytest.mark.asyncio
async def test_web_help_request_is_routed_to_configured_transport() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-help",
            content_hash({"seed": "transport-help"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="help-channel",
                endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                settings={"recipient": "human-ops"},
            ),
            actor="operator",
        )
        interaction = InteractionStore(kernel.database).send(
            kernel.subject_id,
            "web",
            "web-user",
            "Please provide one bounded missing input.",
            kind="help_request",
            idempotency_key="web-help-request",
        )
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"message_id": "help-42"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            delivered = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()
        assert delivered[0].status == "delivered"
        assert requests[0].url == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
        assert interaction.status == "sent"


def test_transport_dns_resolution_rejects_non_public_addresses() -> None:
    try:
        _PublicDNSBackend._public_addresses("127.0.0.1", 443)
    except OSError as error:
        assert "non-public" in str(error)
    else:
        raise AssertionError("private transport address was accepted")


def test_provider_success_http_errors_are_not_recorded_as_delivered() -> None:
    assert DeliveryDispatcher._provider_error("wechat", {"errcode": 40001, "errmsg": "bad"})
    assert DeliveryDispatcher._provider_error("feishu", {"code": 999, "msg": "bad"})
    assert DeliveryDispatcher._provider_error("qq", {"code": 100, "message": "bad"})
    assert DeliveryDispatcher._provider_error("wechat", {"errcode": 0}) is None
    assert DeliveryDispatcher._provider_error("webhook", {"code": 999}) is None


def test_revoked_transport_cannot_be_reenabled_without_new_secret(tmp_path: Path) -> None:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-transport-revocation",
        content_hash({"seed": "transport-revocation"}),
    )
    store = TransportStore(kernel.database, tmp_path / "secrets")
    record = store.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="terminal",
            endpoint="https://example.com/hook",
            credentials={"token": SecretStr("terminal-secret")},
        ),
        actor="operator",
    )
    store.revoke(
        record.transport_id,
        reason="retired",
        actor="operator",
        subject_id=kernel.subject_id,
    )
    with pytest.raises(ValueError, match="revoked"):
        store.enable(
            record.transport_id,
            reason="must not restore",
            actor="operator",
            subject_id=kernel.subject_id,
        )
    assert store.get(record.transport_id, subject_id=kernel.subject_id).status == "revoked"
    assert not (store.secret_dir / f"{record.transport_id}.json").exists()
