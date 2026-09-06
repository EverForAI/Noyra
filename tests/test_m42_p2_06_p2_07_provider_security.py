from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import urlopen

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from noyra.core import SubjectKernel
from noyra.core.http import (
    PublicDNSAsyncHTTPTransport,
    _PublicDNSAsyncBackend,
    _PublicDNSSyncBackend,
)
from noyra.core.types import content_hash
from noyra.model import (
    CognitiveResourceGroupInput,
    CompletionRequest,
    EmbeddingResourceInput,
    EmbeddingResourceStore,
    EmbeddingSettings,
    ModelMessage,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    OpenAIEmbeddingProvider,
)
from noyra.model.errors import EmbeddingProviderError, ProviderCallError
from noyra.service import NoyraHTTPServer, ServiceSettings


class _SyncChunks(httpx.SyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], *, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                time.sleep(self.delay)
            yield chunk

    def close(self) -> None:
        self.closed = True


class _AsyncConnector:
    def __init__(self) -> None:
        self.addresses: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
        del port
        self.addresses.append(host)
        return object()


class _SyncConnector:
    def __init__(self) -> None:
        self.addresses: list[str] = []

    def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
        del port
        self.addresses.append(host)
        return object()


def _dns_answer(address: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    parsed = ipaddress.ip_address(address)
    if parsed.version == 6:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


def _embedding_body(*, tokens: int = 1) -> bytes:
    return json.dumps(
        {
            "data": [{"index": 0, "embedding": [1.0, 0.0]}],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }
    ).encode()


def _embedding_settings(*, timeout_seconds: float = 1.0) -> EmbeddingSettings:
    return EmbeddingSettings(
        base_url="https://embedding.example/v1",
        model="embedding-v1",
        api_key=SecretStr("embedding-secret"),
        timeout_seconds=timeout_seconds,
    )


def _model_request() -> CompletionRequest:
    return CompletionRequest(
        model="model-v1",
        messages=(ModelMessage(role="user", content="Return JSON."),),
        max_output_tokens=32,
        temperature=0.0,
        schema_name="Answer",
        output_schema={"type": "object"},
    )


def test_live_embedding_disable_and_revoke_block_already_built_provider(
    tmp_path: Path,
) -> None:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-p206-live-revoke",
        content_hash({"seed": "p206-live-revoke"}),
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_embedding_body(), request=request)

    store = EmbeddingResourceStore(kernel.database, tmp_path / "secrets")
    record = store.configure(
        kernel.subject_id,
        EmbeddingResourceInput(
            label="live-provider",
            base_url="https://embedding.example/v1",
            model="embedding-v1",
            api_key=SecretStr("embedding-secret"),
        ),
        actor="operator",
    )
    settings = store.active_settings(kernel.subject_id)
    assert settings is not None
    provider = OpenAIEmbeddingProvider(
        settings,
        transport=httpx.MockTransport(handler),
        call_authorizer=store.call_authorizer(kernel.subject_id, settings),
    )

    assert provider.embed(("first call",)) == [[1.0, 0.0]]
    store.disable(
        record.config_id,
        reason="maintenance",
        actor="operator",
        subject_id=kernel.subject_id,
    )

    def invoke(_: int) -> str:
        try:
            provider.embed(("blocked after disable",))
        except EmbeddingProviderError as error:
            return error.code
        return "called"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(invoke, range(16)))
    assert outcomes == ["embedding_resource_inactive"] * 16
    assert calls == 1

    store.enable(
        record.config_id,
        reason="maintenance complete",
        actor="operator",
        subject_id=kernel.subject_id,
    )
    assert provider.embed(("enabled call",)) == [[1.0, 0.0]]
    assert calls == 2
    store.revoke(
        record.config_id,
        reason="retired",
        actor="operator",
        subject_id=kernel.subject_id,
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_resource_inactive"):
        provider.embed(("blocked after revoke",))
    assert calls == 2
    kernel.close()


def test_embedding_secret_delete_failure_is_repaired_and_degrades_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="Noyra-p206-secret-repair",
        genesis_hash=content_hash({"seed": "p206-secret-repair"}),
        host="127.0.0.1",
        port=0,
    )
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        settings.subject_id,
        settings.genesis_hash,
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings, repair_secrets_on_init=False)
    record = server.embedding_resources.configure(
        kernel.subject_id,
        EmbeddingResourceInput(
            label="repair-provider",
            base_url="https://embedding.example/v1",
            model="embedding-v1",
            api_key=SecretStr("secret-value-not-in-queue"),
        ),
        actor="operator",
    )
    secret_path = server.embedding_resources.secret_dir / f"{record.config_id}.key"
    original_secret_path = server.embedding_resources._secret_path

    class _DeleteFailure:
        @staticmethod
        def unlink(*, missing_ok: bool = False) -> None:
            del missing_ok
            raise PermissionError("injected file lock")

    monkeypatch.setattr(
        server.embedding_resources,
        "_secret_path",
        lambda _: cast(Any, _DeleteFailure()),
    )
    server.embedding_resources.revoke(
        record.config_id,
        reason="retired",
        actor="operator",
        subject_id=kernel.subject_id,
    )
    monkeypatch.setattr(server.embedding_resources, "_secret_path", original_secret_path)

    health = server.embedding_resources.cleanup_health(kernel.subject_id)
    assert health["status"] == "degraded"
    assert health["pending"] == 1
    assert health["failed"] == 0
    assert health["max_attempts"] == 0
    assert isinstance(health["oldest_pending_at"], str)
    with kernel.database.connection() as connection:
        row = connection.execute(
            "SELECT resource_type, secret_reference, status, last_error FROM secret_cleanup_queue"
        ).fetchone()
    assert dict(row) == {
        "resource_type": "embedding",
        "secret_reference": f"{record.config_id}.key",
        "status": "pending",
        "last_error": "PermissionError",
    }
    assert "secret-value-not-in-queue" not in json.dumps(dict(row))
    assert secret_path.exists()

    server.start()
    _, port = server.address
    with pytest.raises(HTTPError) as caught:
        urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
    assert caught.value.code == 503
    payload = json.loads(caught.value.read())
    assert payload["status"] == "degraded"
    assert payload["embedding_secret_cleanup"]["pending"] == 1
    assert payload["secret_cleanup"]["pending"] == 1
    assert payload["secret_cleanup"]["domains"]["embedding"]["pending"] == 1
    assert "secret_reference" not in json.dumps(payload)

    restarted = EmbeddingResourceStore(kernel.database, server.embedding_resources.secret_dir)
    assert restarted.cleanup_health(kernel.subject_id)["status"] == "ok"
    assert not secret_path.exists()
    server.close()
    kernel.close()


@pytest.mark.asyncio
async def test_public_dns_is_pinned_for_model_and_embedding_redirects_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_model_provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            base_url="https://model.example/v1",
            model="model-v1",
            api_key=SecretStr("model-secret"),
        )
    )
    assert isinstance(default_model_provider._client._transport, PublicDNSAsyncHTTPTransport)
    await default_model_provider.aclose()

    answers = {
        "model.example": iter((_dns_answer("8.8.8.8"), _dns_answer("127.0.0.1"))),
        "redirect.example": iter((_dns_answer("127.0.0.1"),)),
        "embedding.example": iter((_dns_answer("2606:4700:4700::1111"), _dns_answer("10.0.0.8"))),
    }

    def resolve(host: str, *_: Any, **__: Any) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return next(answers[host])

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    async_backend = _PublicDNSAsyncBackend()
    async_connector = _AsyncConnector()
    async_backend._backend = cast(Any, async_connector)
    await async_backend.connect_tcp("model.example", 443)
    with pytest.raises(OSError, match="non-public"):
        await async_backend.connect_tcp("model.example", 443)
    with pytest.raises(OSError, match="non-public"):
        await async_backend.connect_tcp("redirect.example", 443)
    assert async_connector.addresses == ["8.8.8.8"]

    sync_backend = _PublicDNSSyncBackend()
    sync_connector = _SyncConnector()
    sync_backend._backend = cast(Any, sync_connector)
    sync_backend.connect_tcp("embedding.example", 443)
    with pytest.raises(OSError, match="non-public"):
        sync_backend.connect_tcp("embedding.example", 443)
    assert sync_connector.addresses == ["2606:4700:4700::1111"]

    requests: list[str] = []

    async def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://redirect.example/private"},
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(redirect),
        follow_redirects=True,
    )
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            base_url="https://model.example/v1",
            model="model-v1",
            api_key=SecretStr("model-secret"),
        ),
        client=client,
    )
    try:
        with pytest.raises(ProviderCallError, match="provider_http_302"):
            await provider.complete(_model_request())
    finally:
        await client.aclose()
    assert requests == ["https://model.example/v1/chat/completions"]

    embedding_requests: list[str] = []

    def embedding_redirect(request: httpx.Request) -> httpx.Response:
        embedding_requests.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://redirect.example/private"},
            request=request,
        )

    embedding_provider = OpenAIEmbeddingProvider(
        _embedding_settings(),
        transport=httpx.MockTransport(embedding_redirect),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_http_307"):
        embedding_provider.embed(("redirect must not be followed",))
    assert embedding_requests == ["https://embedding.example/v1/embeddings"]


def test_embedding_response_stream_has_hard_body_time_and_token_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_stream = _SyncChunks((b"ignored",))
    requested_encodings: list[str] = []

    def oversized(request: httpx.Request) -> httpx.Response:
        requested_encodings.append(request.headers["accept-encoding"])
        return httpx.Response(
            200,
            headers={"Content-Length": "2000001"},
            stream=oversized_stream,
            request=request,
        )

    oversized_provider = OpenAIEmbeddingProvider(
        _embedding_settings(),
        transport=httpx.MockTransport(oversized),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_response_too_large"):
        oversized_provider.embed(("bounded",))
    assert oversized_stream.closed is True
    assert requested_encodings == ["identity"]

    slow_stream = _SyncChunks((_embedding_body(), b" "), delay=0.03)

    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=slow_stream, request=request)

    slow_provider = OpenAIEmbeddingProvider(
        _embedding_settings(timeout_seconds=0.05),
        transport=httpx.MockTransport(slow),
    )
    started = time.monotonic()
    with pytest.raises(EmbeddingProviderError, match="embedding_timeout"):
        slow_provider.embed(("slow bounded response",))
    assert time.monotonic() - started < 0.5
    assert slow_stream.closed is True

    token_stream = _SyncChunks((_embedding_body(tokens=10_000),))

    def excessive_tokens(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=token_stream, request=request)

    token_provider = OpenAIEmbeddingProvider(
        _embedding_settings(),
        transport=httpx.MockTransport(excessive_tokens),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_response_token_limit"):
        token_provider.embed(("tiny",))
    assert token_stream.closed is True

    calls = 0

    def success(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_embedding_body(), request=request)

    factory = httpx.MockTransport(success)
    monkeypatch.setattr("noyra.model.embedding.PublicDNSHTTPTransport", lambda: factory)
    default_provider = OpenAIEmbeddingProvider(_embedding_settings())
    assert default_provider.embed(("default pinned transport",)) == [[1.0, 0.0]]
    assert calls == 1


def test_operator_custom_endpoints_reject_literal_private_targets() -> None:
    with pytest.raises(ValidationError):
        EmbeddingSettings(
            base_url="https://127.0.0.1/v1",
            model="embedding-v1",
            api_key=SecretStr("secret"),
        )
    with pytest.raises(ValidationError):
        OpenAICompatibleSettings(
            base_url="https://127.0.0.1/v1",
            model="model-v1",
            api_key=SecretStr("secret"),
        )
    with pytest.raises(ValidationError):
        OpenAICompatibleSettings(
            base_url="http://localhost:11434/v1",
            model="model-v1",
            api_key=SecretStr("secret"),
        )
    local = OpenAICompatibleSettings(
        base_url="http://localhost:11434/v1",
        model="model-v1",
        api_key=SecretStr("secret"),
        allow_local_endpoint=True,
    )
    assert local.allow_local_endpoint is True

    with pytest.raises(ValidationError):
        CognitiveResourceGroupInput(
            pool="deep",
            label="private-target",
            base_url="https://10.0.0.8/v1",
            model="model-v1",
            api_keys=(SecretStr("secret"),),
        )
    with pytest.raises(ValidationError):
        CognitiveResourceGroupInput(
            pool="deep",
            label="local-target",
            base_url="http://localhost:11434/v1",
            model="model-v1",
            api_keys=(SecretStr("secret"),),
        )
