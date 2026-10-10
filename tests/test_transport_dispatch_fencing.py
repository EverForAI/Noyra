from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.admission import OperationInvalidated, RuntimeAdmissionGate, bind_lease
from noyra.core.types import content_hash
from noyra.interaction import DeliveryDispatcher, InteractionStore, TransportInput, TransportStore
from noyra.interaction.transport import (
    TRANSPORT_MAX_RESPONSE_BYTES,
    TransportChannel,
    TransportRecord,
)


def setup(tmp_path: Path, channel: str) -> tuple[Any, ...]:
    db = Database(tmp_path / "test.sqlite3")
    subject = "dispatch"
    IdentityStore(db).ensure(subject, content_hash(subject))
    gate = RuntimeAdmissionGate(subject)
    gate.open()
    store = TransportStore(db, tmp_path / "secrets")
    record = store.configure(
        subject,
        TransportInput(
            channel=cast(TransportChannel, channel),
            label="synthetic",
            endpoint={
                "qq": "https://api.sgroup.qq.com",
                "feishu": "https://open.feishu.cn",
                "wechat": "https://api.weixin.qq.com/cgi-bin/message/custom/send",
            }[channel],
            credentials={"app_id": SecretStr("synthetic-id"), "app_secret": SecretStr("synthetic")},
        ),
        actor="operator",
    )
    InteractionStore(db).send(subject, channel, "recipient", "hello")
    return db, subject, gate, store, record


def token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "synthetic-token",
            "tenant_access_token": "synthetic-token",
            "expires_in": 7200,
            "expire": 7200,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["qq", "feishu", "wechat"])
@pytest.mark.parametrize("phase", ["lock", "token_headers", "message_headers"])
@pytest.mark.parametrize("withdraw", ["lease", "disable", "credential"])
async def test_withdrawal_stops_token_and_message_after_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, phase: str, withdraw: str
) -> None:
    db, subject, gate, store, record = setup(tmp_path, channel)
    sends: list[str] = []
    entered = asyncio.Event()

    def invalidate() -> None:
        if withdraw == "lease":
            gate.invalidate()
        elif withdraw == "disable":
            store.disable(record.transport_id, subject_id=subject, actor="operator", reason="test")
        else:
            path = tmp_path / "secrets" / f"{record.transport_id}.json"
            data = json.loads(path.read_text())
            data["credentials"]["app_secret"] = "rotated-synthetic"
            path.write_text(json.dumps(data))

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            name = "token" if "token" in request.url.path.lower() else "message"
            if phase == name + "_headers":
                invalidate()
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(name)
            return token_response() if name == "token" else httpx.Response(200, json={"id": "sent"})

    async with httpx.AsyncClient(transport=Transport()) as client:
        dispatcher = DeliveryDispatcher(db, store, client=client)
        original = dispatcher._provider_token

        async def observed(*args: Any, **kwargs: Any) -> str:
            entered.set()
            return await original(*args, **kwargs)

        monkeypatch.setattr(dispatcher, "_provider_token", observed)
        if phase == "lock":
            await dispatcher._provider_token_lock.acquire()
        with gate.operation("test") as lease, bind_lease(lease):
            task = asyncio.create_task(dispatcher.deliver_pending(subject))
            if phase == "lock":
                await asyncio.wait_for(entered.wait(), 5)
                invalidate()
                dispatcher._provider_token_lock.release()
            if withdraw == "lease":
                with pytest.raises(OperationInvalidated):
                    await task
            else:
                result = await task
                assert result[0].status == "failed"
                assert result[0].attempts >= 3 and result[0].next_retry_at is None
        assert sends == (["token"] if phase == "message_headers" else [])
        assert await dispatcher.deliver_pending(subject) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["success", "unknown", "expired"])
async def test_sent_receipt_survives_invalidation_without_refresh_or_retry(
    tmp_path: Path, response: str
) -> None:
    db, subject, gate, store, _record = setup(tmp_path, "qq")
    sends: list[str] = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(request.url.path)
            if request.url.host == "bots.qq.com":
                return token_response()
            gate.invalidate()
            if response == "unknown":
                raise httpx.ReadTimeout("synthetic response timeout")
            if response == "expired":
                return httpx.Response(401)
            return httpx.Response(200, json={"id": "receipt-1"})

    async with httpx.AsyncClient(transport=Transport()) as client:
        dispatcher = DeliveryDispatcher(db, store, client=client)
        with (
            gate.operation("test") as lease,
            bind_lease(lease),
            pytest.raises(OperationInvalidated),
        ):
            await dispatcher.deliver_pending(subject)
        assert len(sends) == 2
        with db.connection() as connection:
            row = connection.execute("SELECT * FROM interaction_deliveries").fetchone()
        assert row["status"] == ("delivered" if response == "success" else "unknown")
        if response == "success":
            assert row["provider_message_id"] == "receipt-1"
        assert await dispatcher.deliver_pending(subject) == []


@pytest.mark.asyncio
async def test_provider_token_body_is_bounded_while_streaming(tmp_path: Path) -> None:
    db, subject, _gate, store, _record = setup(tmp_path, "qq")
    consumed = 0
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal consumed
            for _ in range(128):
                consumed += 65536
                yield b"x" * 65536

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    ) as client:
        result = await DeliveryDispatcher(db, store, client=client).deliver_pending(subject)
    assert result[0].status == "failed"
    assert consumed <= TRANSPORT_MAX_RESPONSE_BYTES + 65536
    assert closed


@pytest.mark.asyncio
async def test_token_lock_wait_uses_request_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, subject, _gate, store, _record = setup(tmp_path, "qq")
    monkeypatch.setattr("noyra.interaction.transport.TRANSPORT_TOTAL_TIMEOUT_SECONDS", 0.02)
    sends = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return token_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = DeliveryDispatcher(db, store, client=client)
        await dispatcher._provider_token_lock.acquire()
        try:
            result = await asyncio.wait_for(dispatcher.deliver_pending(subject), 10)
        finally:
            dispatcher._provider_token_lock.release()
    assert result[0].status == "failed" and sends == []


@pytest.mark.asyncio
async def test_token_cache_is_scoped_to_credentials_and_still_checks_authorization() -> None:
    record = TransportRecord(
        "transport", "subject", "qq", "test", "https://api.sgroup.qq.com", "active", "now", "now"
    )
    calls = []
    checks = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return token_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = DeliveryDispatcher(
            cast(Database, None), cast(TransportStore, None), client=client
        )
        for secret in ("first", "first", "second"):
            await dispatcher._provider_token(
                record,
                record.transport_id,
                "qq",
                {"app_id": "synthetic", "app_secret": secret},
                lambda: checks.append(True),
            )
        assert len(calls) == 2 and len(checks) >= 3
        assert len(dispatcher._provider_tokens) == 1

        def revoked() -> None:
            raise PermissionError("withdrawn")

        with pytest.raises(PermissionError):
            await dispatcher._provider_token(
                record,
                record.transport_id,
                "qq",
                {"app_id": "synthetic", "app_secret": "second"},
                revoked,
            )
        assert len(calls) == 2
