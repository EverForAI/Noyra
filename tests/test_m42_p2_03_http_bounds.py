from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from noyra.core import SubjectKernel
from noyra.core.database import Database
from noyra.core.http import HTTPResponseLimitError, read_bounded_response
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore, TransportInput, TransportStore
from noyra.interaction.transport import DeliveryDispatcher, TransportRecord
from noyra.research.browser import BrowserSearchExecutor, BrowserSearchOutcomeUnknownError
from noyra.research.search import SearchExecutor, SearchOutcomeUnknownError
from noyra.research.types import SearchProviderRecord


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], *, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _response(
    stream: httpx.AsyncByteStream,
    *,
    headers: dict[str, str] | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers=headers,
        stream=stream,
        request=request or httpx.Request("GET", "https://example.com/resource"),
    )


@pytest.mark.asyncio
async def test_bounded_reader_enforces_body_header_and_encoding_limits() -> None:
    oversized = _response(_ChunkStream((b"0123456789",)))
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(oversized, max_body_bytes=8)

    crossing = _response(_ChunkStream((b"1234", b"56789")))
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(crossing, max_body_bytes=8)

    exact = _response(_ChunkStream((b"1234", b"5678")))
    assert await read_bounded_response(exact, max_body_bytes=8) == b"12345678"

    declared = _response(_ChunkStream((b"",)), headers={"Content-Length": "9"})
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(declared, max_body_bytes=8)

    invalid_length = _response(_ChunkStream((b"",)), headers={"Content-Length": "invalid"})
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(invalid_length, max_body_bytes=8)

    header_response = _response(
        _ChunkStream((b"ok",)),
        headers={"X- oversized": "x" * 32},
    )
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(header_response, max_header_bytes=16)

    encoded = _response(
        _ChunkStream((b"compressed",)),
        headers={"Content-Encoding": "gzip"},
    )
    with pytest.raises(HTTPResponseLimitError):
        await read_bounded_response(encoded, max_body_bytes=32)


@pytest.mark.asyncio
async def test_bounded_reader_turns_slow_stream_into_read_timeout() -> None:
    response = _response(_ChunkStream((b"slow",), delay=0.05))
    with pytest.raises(httpx.ReadTimeout):
        await read_bounded_response(response, total_timeout_seconds=0.01)


def _search_config() -> SearchProviderRecord:
    return SearchProviderRecord(
        "searchcfg-test",
        "subject-test",
        "brave",
        "test",
        "fingerprint",
        {},
        10,
        "active",
        "2026-01-01T00:00:00+00:00",
        None,
        None,
    )


class _ProviderSecrets:
    @staticmethod
    def api_key(_: str, *, subject_id: str) -> str:
        assert subject_id == "subject-test"
        return "test-key"


@pytest.mark.asyncio
async def test_search_stream_bounds_release_response_and_preserve_unknown_timeout() -> None:
    oversized_stream = _ChunkStream((b"x" * 2_000_001,))

    async def oversized(request: httpx.Request) -> httpx.Response:
        return _response(oversized_stream, request=request)

    oversized_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    executor = SearchExecutor(
        cast(Database, None),
        cast(Any, _ProviderSecrets()),
        client=oversized_client,
    )
    try:
        with pytest.raises(HTTPResponseLimitError):
            await executor._request(_search_config(), "bounded", 2)
    finally:
        await oversized_client.aclose()
    assert oversized_stream.closed is True

    slow_stream = _ChunkStream((b"slow",), delay=0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        return _response(slow_stream, request=request)

    slow_client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    executor = SearchExecutor(
        cast(Database, None),
        cast(Any, _ProviderSecrets()),
        client=slow_client,
    )
    try:
        with (
            patch("noyra.research.search.DEFAULT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(SearchOutcomeUnknownError),
        ):
            await executor._request(_search_config(), "slow", 2)
    finally:
        await slow_client.aclose()
    assert slow_stream.closed is True


@pytest.mark.asyncio
async def test_browser_stream_bounds_and_unknown_timeout() -> None:
    oversized_stream = _ChunkStream((b"x" * 2_000_001,))

    async def oversized(request: httpx.Request) -> httpx.Response:
        return _response(oversized_stream, request=request)

    oversized_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    executor = BrowserSearchExecutor(cast(Database, None), client=oversized_client)
    try:
        with pytest.raises(HTTPResponseLimitError):
            await executor._request("bounded", 2)
    finally:
        await oversized_client.aclose()
    assert oversized_stream.closed is True

    slow_stream = _ChunkStream((b"slow",), delay=0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        return _response(slow_stream, request=request)

    slow_client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    executor = BrowserSearchExecutor(cast(Database, None), client=slow_client)
    try:
        with (
            patch("noyra.research.browser.DEFAULT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(BrowserSearchOutcomeUnknownError),
        ):
            await executor._request("slow", 2)
    finally:
        await slow_client.aclose()
    assert slow_stream.closed is True


@pytest.mark.asyncio
async def test_transport_stream_bounds_and_unknown_timeout() -> None:
    record = TransportRecord(
        "transport-test",
        "subject-test",
        "webhook",
        "test",
        "https://hooks.example.test/notify",
        "active",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    interaction: dict[str, Any] = {"counterparty": "recipient", "content": "message"}

    oversized_stream = _ChunkStream((b"x" * 2_000_001,))

    async def oversized(request: httpx.Request) -> httpx.Response:
        return _response(oversized_stream, request=request)

    oversized_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    dispatcher = DeliveryDispatcher(cast(Database, None), cast(Any, None), client=oversized_client)
    try:
        with pytest.raises(HTTPResponseLimitError):
            await dispatcher._send(record, {}, interaction, "delivery-test")
    finally:
        await oversized_client.aclose()
    assert oversized_stream.closed is True

    slow_stream = _ChunkStream((b"slow",), delay=0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        return _response(slow_stream, request=request)

    slow_client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    dispatcher = DeliveryDispatcher(cast(Database, None), cast(Any, None), client=slow_client)
    try:
        with (
            patch("noyra.interaction.transport.TRANSPORT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(httpx.ReadTimeout),
        ):
            await dispatcher._send(record, {}, interaction, "delivery-test")
    finally:
        await slow_client.aclose()
    assert slow_stream.closed is True


@pytest.mark.asyncio
async def test_oversized_transport_response_is_unknown_and_not_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kernel = SubjectKernel(
            root / "noyra.sqlite3",
            "Noyra-transport-bounds",
            content_hash({"seed": "transport-bounds"}),
        )
        transports = TransportStore(kernel.database, root / "secrets")
        transports.configure(
            kernel.subject_id,
            TransportInput(
                channel="feishu",
                label="bounded",
                endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            ),
            actor="operator",
        )
        InteractionStore(kernel.database).send(
            kernel.subject_id,
            "feishu",
            "team",
            "A bounded delivery.",
        )
        stream = _ChunkStream((b"x" * 2_000_001,))
        requests: list[httpx.Request] = []

        async def oversized(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _response(stream, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
        dispatcher = DeliveryDispatcher(kernel.database, transports, client=client)
        try:
            delivered = await dispatcher.deliver_pending(kernel.subject_id)
            retried = await dispatcher.deliver_pending(kernel.subject_id)
        finally:
            await client.aclose()

        assert delivered[0].status == "unknown"
        assert retried == []
        assert len(requests) == 1
        assert stream.closed is True


@pytest.mark.asyncio
async def test_adapter_deadlines_include_waiting_for_response_headers() -> None:
    async def delayed_headers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, request=request)

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(delayed_headers))
    search = SearchExecutor(
        cast(Database, None),
        cast(Any, _ProviderSecrets()),
        client=search_client,
    )
    try:
        with (
            patch("noyra.research.search.DEFAULT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(SearchOutcomeUnknownError),
        ):
            await search._request(_search_config(), "slow headers", 2)
    finally:
        await search_client.aclose()

    browser_client = httpx.AsyncClient(transport=httpx.MockTransport(delayed_headers))
    browser = BrowserSearchExecutor(cast(Database, None), client=browser_client)
    try:
        with (
            patch("noyra.research.browser.DEFAULT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(BrowserSearchOutcomeUnknownError),
        ):
            await browser._request("slow headers", 2)
    finally:
        await browser_client.aclose()

    record = TransportRecord(
        "transport-test",
        "subject-test",
        "webhook",
        "test",
        "https://hooks.example.test/notify",
        "active",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    interaction: dict[str, Any] = {"counterparty": "recipient", "content": "message"}
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(delayed_headers))
    dispatcher = DeliveryDispatcher(cast(Database, None), cast(Any, None), client=transport_client)
    try:
        with (
            patch("noyra.interaction.transport.TRANSPORT_TOTAL_TIMEOUT_SECONDS", 0.01),
            pytest.raises(httpx.ReadTimeout),
        ):
            await dispatcher._send(record, {}, interaction, "delivery-test")
    finally:
        await transport_client.aclose()


@pytest.mark.asyncio
async def test_adapters_disable_redirects_per_request() -> None:
    search_requests: list[httpx.Request] = []

    async def search_redirect(request: httpx.Request) -> httpx.Response:
        search_requests.append(request)
        if len(search_requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://redirect.example.test/search"},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    search_client = httpx.AsyncClient(
        transport=httpx.MockTransport(search_redirect), follow_redirects=True
    )
    search = SearchExecutor(
        cast(Database, None),
        cast(Any, _ProviderSecrets()),
        client=search_client,
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await search._request(_search_config(), "no redirect", 2)
    finally:
        await search_client.aclose()
    assert len(search_requests) == 1
    assert search_requests[0].headers["accept-encoding"] == "identity"

    browser_requests: list[httpx.Request] = []

    async def browser_redirect(request: httpx.Request) -> httpx.Response:
        browser_requests.append(request)
        if len(browser_requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://redirect.example.test/browser"},
                request=request,
            )
        return httpx.Response(200, content=b"<rss/>", request=request)

    browser_client = httpx.AsyncClient(
        transport=httpx.MockTransport(browser_redirect), follow_redirects=True
    )
    browser = BrowserSearchExecutor(cast(Database, None), client=browser_client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await browser._request("no redirect", 2)
    finally:
        await browser_client.aclose()
    assert len(browser_requests) == 1
    assert browser_requests[0].headers["accept-encoding"] == "identity"

    transport_requests: list[httpx.Request] = []

    async def transport_redirect(request: httpx.Request) -> httpx.Response:
        transport_requests.append(request)
        if len(transport_requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://redirect.example.test/transport"},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    transport_client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_redirect), follow_redirects=True
    )
    dispatcher = DeliveryDispatcher(cast(Database, None), cast(Any, None), client=transport_client)
    record = TransportRecord(
        "transport-test",
        "subject-test",
        "webhook",
        "test",
        "https://hooks.example.test/notify",
        "active",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    interaction: dict[str, Any] = {"counterparty": "recipient", "content": "message"}
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await dispatcher._send(record, {}, interaction, "delivery-test")
    finally:
        await transport_client.aclose()
    assert len(transport_requests) == 1
    assert transport_requests[0].headers["accept-encoding"] == "identity"
