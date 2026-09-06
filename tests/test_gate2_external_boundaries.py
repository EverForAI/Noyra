from __future__ import annotations

import asyncio
import hashlib
import io
import sys
import threading
import time
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any, cast

import httpcore
import httpx
import pytest
from pydantic import SecretStr

import noyra.core.http as http_boundary
from noyra.core.archive import S3ArchiveProvider
from noyra.core.http import SyncHTTPTimeoutHook, _PublicDNSAsyncBackend
from noyra.core.types import content_hash
from noyra.model import (
    CompletionRequest,
    ModelMessage,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
)
from noyra.model.errors import ProviderCallError
from noyra.world import SafeWebReader
from noyra.world.errors import FetchError
from noyra.world.types import SourceRecord


class _AsyncConnector:
    def __init__(self) -> None:
        self.addresses: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
        del port
        self.addresses.append(host)
        return _NetworkStream()


class _NetworkStream:
    def get_extra_info(self, _: str) -> None:
        return None


class _SlowAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], delay: float) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _CloseProbe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_sync_http_timeout_hook_closes_active_and_late_registered_resources() -> None:
    active = _CloseProbe()
    hook = SyncHTTPTimeoutHook()
    hook.register(active)

    def blocked() -> None:
        time.sleep(0.05)

    with pytest.raises(TimeoutError):
        http_boundary.call_sync_http_with_deadline(
            blocked,
            timeout=0.01,
            on_timeout=hook.cancel,
        )
    assert active.closed is True

    late = _CloseProbe()
    hook.register(late)
    assert late.closed is True


def _model_request() -> CompletionRequest:
    return CompletionRequest(
        model="model-v1",
        messages=(ModelMessage(role="user", content="Return JSON."),),
        max_output_tokens=32,
        temperature=0.0,
        schema_name="Answer",
        output_schema={"type": "object"},
    )


def _source() -> SourceRecord:
    return SourceRecord(
        "source-gate2",
        "subject-gate2",
        "Gate 2 source",
        "https://world.example/source",
        "news",
        0.8,
        "active",
        content_hash({"source": "gate2"}),
        1,
        "2026-08-19T00:00:00.000+00:00",
        "2026-08-19T00:00:00.000+00:00",
    )


@pytest.mark.asyncio
async def test_custom_resolver_result_is_the_exact_tcp_destination() -> None:
    calls = 0

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        assert (host, port) == ("world.example", 443)
        calls += 1
        return ("93.184.216.34",)

    backend = _PublicDNSAsyncBackend(resolver)
    connector = _AsyncConnector()
    backend._backend = cast(Any, connector)
    await backend.connect_tcp("world.example", 443)

    assert calls == 1
    assert connector.addresses == ["93.184.216.34"]

    async def rebound(_: str, __: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    rebound_backend = _PublicDNSAsyncBackend(rebound)
    rebound_backend._backend = cast(Any, connector)
    with pytest.raises(OSError, match="non-public"):
        await rebound_backend.connect_tcp("world.example", 443)
    assert connector.addresses == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_default_async_dns_deadlines_do_not_fill_the_shared_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    four_started = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def blocking_resolver(_: str, __: int) -> list[str]:
        nonlocal calls
        with call_lock:
            calls += 1
            if calls == 4:
                four_started.set()
        assert release.wait(5)
        return ["93.184.216.34"]

    monkeypatch.setattr(http_boundary, "public_addresses", blocking_resolver)
    tasks = [
        asyncio.create_task(
            _PublicDNSAsyncBackend().connect_tcp(
                f"blocked-{index}.example",
                443,
                timeout=0.05,
            )
        )
        for index in range(4)
    ]
    try:
        wait_deadline = asyncio.get_running_loop().time() + 1
        while not four_started.is_set() and asyncio.get_running_loop().time() < wait_deadline:
            await asyncio.sleep(0.005)
        assert four_started.is_set()

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(error, httpcore.ConnectTimeout) for error in outcomes)

        with pytest.raises(httpcore.ConnectTimeout, match="DNS resolution timed out"):
            await _PublicDNSAsyncBackend().connect_tcp(
                "capacity.example",
                443,
                timeout=0.02,
            )
        assert calls == 4
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_model_deadline_includes_headers_and_slow_response_stream() -> None:
    async def delayed_headers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, request=request)

    settings = OpenAICompatibleSettings(
        base_url="https://model.example/v1",
        model="model-v1",
        api_key=SecretStr("model-secret"),
        timeout_seconds=0.01,
        connect_timeout_seconds=0.005,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(delayed_headers))
    provider = OpenAICompatibleProvider(settings, client=client)
    started = time.monotonic()
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(_model_request())
    finally:
        await client.aclose()
    assert caught.value.code == "provider_outcome_unknown"
    assert caught.value.outcome_unknown is True
    assert time.monotonic() - started < 0.5

    stream = _SlowAsyncStream(
        (
            b'{"choices":[{"message":{"content":"{}"}}],',
            b'"usage":{"prompt_tokens":1,"completion_tokens":1}}',
        ),
        0.03,
    )

    async def slow_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    slow_settings = settings.model_copy(
        update={"timeout_seconds": 0.05, "connect_timeout_seconds": 0.01}
    )
    slow_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_body))
    slow_provider = OpenAICompatibleProvider(slow_settings, client=slow_client)
    try:
        with pytest.raises(ProviderCallError, match="provider_outcome_unknown"):
            await slow_provider.complete(_model_request())
    finally:
        await slow_client.aclose()
    assert stream.closed is True


@pytest.mark.asyncio
async def test_model_requests_identity_encoding_and_keeps_rate_limit_retryable() -> None:
    captured_headers: httpx.Headers | None = None

    async def rate_limited(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = request.headers
        return httpx.Response(429, request=request)

    settings = OpenAICompatibleSettings(
        base_url="https://model.example/v1",
        model="model-v1",
        api_key=SecretStr("model-secret"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(rate_limited))
    provider = OpenAICompatibleProvider(settings, client=client)
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(_model_request())
    finally:
        await client.aclose()

    assert captured_headers is not None
    assert captured_headers["accept-encoding"] == "identity"
    assert caught.value.code == "provider_http_429"
    assert caught.value.retryable is True
    assert caught.value.outcome_unknown is False
    assert caught.value.usage_unknown is False


@pytest.mark.parametrize("status_code", [408, 409, 425, 500, 503])
@pytest.mark.asyncio
async def test_model_ambiguous_post_http_status_is_not_retried(status_code: int) -> None:
    async def ambiguous(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    settings = OpenAICompatibleSettings(
        base_url="https://model.example/v1",
        model="model-v1",
        api_key=SecretStr("model-secret"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(ambiguous))
    provider = OpenAICompatibleProvider(settings, client=client)
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(_model_request())
    finally:
        await client.aclose()

    assert caught.value.code == f"provider_http_{status_code}"
    assert caught.value.retryable is False
    assert caught.value.outcome_unknown is True
    assert caught.value.usage_unknown is True


@pytest.mark.asyncio
async def test_model_declared_response_overflow_has_unknown_outcome() -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "2048"},
            content=b"x",
            request=request,
        )

    settings = OpenAICompatibleSettings(
        base_url="https://model.example/v1",
        model="model-v1",
        api_key=SecretStr("model-secret"),
        max_response_bytes=1_024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    provider = OpenAICompatibleProvider(settings, client=client)
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(_model_request())
    finally:
        await client.aclose()

    assert caught.value.code == "provider_response_too_large"
    assert caught.value.retryable is False
    assert caught.value.outcome_unknown is True
    assert caught.value.usage_unknown is True


@pytest.mark.parametrize(
    ("body", "error_code"),
    [(b"not-json", "provider_response_invalid"), (b'{"choices":[]}', "provider_response_empty")],
)
@pytest.mark.asyncio
async def test_model_accepted_but_invalid_response_is_outcome_unknown(
    body: bytes,
    error_code: str,
) -> None:
    async def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    settings = OpenAICompatibleSettings(
        base_url="https://model.example/v1",
        model="model-v1",
        api_key=SecretStr("model-secret"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    provider = OpenAICompatibleProvider(settings, client=client)
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(_model_request())
    finally:
        await client.aclose()

    assert caught.value.code == error_code
    assert caught.value.retryable is False
    assert caught.value.outcome_unknown is True
    assert caught.value.usage_unknown is True


@pytest.mark.asyncio
async def test_world_deadline_includes_headers_and_closes_slow_stream() -> None:
    async def resolver(_: str, __: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def delayed_headers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="late",
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(delayed_headers))
    reader = SafeWebReader(
        client=client,
        resolver=resolver,
        verify_peer_address=False,
        total_timeout_seconds=0.01,
    )
    try:
        with pytest.raises(FetchError, match="world_fetch_timeout"):
            await reader.fetch(_source())
    finally:
        await client.aclose()

    stream = _SlowAsyncStream((b"first", b"second"), 0.03)

    async def slow_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=stream,
            request=request,
        )

    slow_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_body))
    slow_reader = SafeWebReader(
        client=slow_client,
        resolver=resolver,
        verify_peer_address=False,
        total_timeout_seconds=0.05,
    )
    try:
        with pytest.raises(FetchError, match="world_fetch_timeout"):
            await slow_reader.fetch(_source())
    finally:
        await slow_client.aclose()
    assert stream.closed is True


class _S3Client:
    def __init__(
        self,
        *,
        ready: bool = True,
        delay: float = 0.0,
        bucket_owner: str | None = None,
    ) -> None:
        self.ready = ready
        self.delay = delay
        self.bucket_owner = bucket_owner
        self.head_bucket_calls = 0
        self.put_object_calls = 0
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_kwargs: dict[str, Any] = {}

    def _check_owner(self, kwargs: dict[str, Any]) -> None:
        self.last_kwargs = dict(kwargs)
        if self.bucket_owner is not None and kwargs.get("ExpectedBucketOwner") != self.bucket_owner:
            raise OSError("bucket owner mismatch")

    def head_bucket(self, **kwargs: Any) -> None:
        self._check_owner(kwargs)
        self.head_bucket_calls += 1
        if not self.ready:
            raise OSError("offline")

    def put_object(self, **kwargs: Any) -> None:
        self._check_owner(kwargs)
        self.put_object_calls += 1
        if self.delay:
            time.sleep(self.delay)
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(kwargs["Body"])

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._check_owner(kwargs)
        payload = self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]
        return {"Body": io.BytesIO(payload), "Metadata": {}}

    def head_object(self, **kwargs: Any) -> None:
        self._check_owner(kwargs)
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]


class _BlockingS3Body:
    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def read(self, *_: Any) -> bytes:
        self.read_started.set()
        self.closed.wait(5)
        return b""

    def close(self) -> None:
        self.closed.set()


class _OversizedS3Body:
    def __init__(self) -> None:
        self.closed = False

    def read(self, _: int) -> bytes:
        return b"x" * 10_000

    def close(self) -> None:
        self.closed = True


class _BlockingBodyS3Client(_S3Client):
    def __init__(self, body: _BlockingS3Body) -> None:
        super().__init__()
        self.body = body

    def get_object(self, **_: Any) -> dict[str, Any]:
        return {"Body": self.body, "Metadata": {}}


class _OversizedBodyS3Client(_S3Client):
    def __init__(self, body: _OversizedS3Body) -> None:
        super().__init__()
        self.body = body

    def get_object(self, **_: Any) -> dict[str, Any]:
        return {"Body": self.body, "Metadata": {}}


class _TransferThreadTrapS3Client(_S3Client):
    def upload_fileobj(self, **_: Any) -> None:
        raise AssertionError("s3transfer must not run outside Noyra's bounded SDK worker")


def test_s3_provider_identity_binds_endpoint_region_and_account() -> None:
    client = _S3Client()
    first = S3ArchiveProvider(
        client,
        bucket="archive",
        prefix="subject",
        endpoint_url="https://S3.EXAMPLE:443/",
        region_name="EU-WEST-1",
        account_id="account-a",
    )
    endpoint_changed = S3ArchiveProvider(
        client,
        bucket="archive",
        prefix="subject",
        endpoint_url="https://other.example",
        region_name="eu-west-1",
        account_id="account-a",
    )
    account_changed = S3ArchiveProvider(
        client,
        bucket="archive",
        prefix="subject",
        endpoint_url="https://s3.example",
        region_name="eu-west-1",
        account_id="account-b",
    )

    assert first.endpoint_url == "https://s3.example"
    assert first.region_name == "eu-west-1"
    assert len({first.provider_id, endpoint_changed.provider_id, account_changed.provider_id}) == 3


def test_s3_requests_bind_expected_bucket_owner_and_readiness_fails_on_mismatch() -> None:
    client = _S3Client(bucket_owner="account-a")
    provider = S3ArchiveProvider(
        client,
        bucket="archive",
        account_id="account-a",
        attempts=1,
    )
    assert provider.readiness(force=True)["ready"] is True
    provider.put("cold/owner.bin", b"owner-bound")
    assert client.last_kwargs["ExpectedBucketOwner"] == "account-a"
    assert provider.get("cold/owner.bin") == b"owner-bound"

    wrong = S3ArchiveProvider(
        _S3Client(bucket_owner="account-b"),
        bucket="archive",
        account_id="account-a",
        attempts=1,
    )
    readiness = wrong.readiness(force=True)
    assert readiness["ready"] is False
    assert readiness["error"] == "OSError"


def test_s3_from_env_wires_botocore_timeouts_retries_pool_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    client = _S3Client(bucket_owner="123456789012")

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured["config"] = kwargs

    boto3_module = ModuleType("boto3")

    def create_client(service: str, **kwargs: Any) -> _S3Client:
        captured["service"] = service
        captured["client"] = kwargs
        return client

    boto3_module.client = create_client  # type: ignore[attr-defined]
    botocore_config_module = ModuleType("botocore.config")
    botocore_config_module.Config = FakeConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3_module)
    botocore_package = ModuleType("botocore")
    cast(Any, botocore_package).__path__ = []
    monkeypatch.setitem(sys.modules, "botocore", botocore_package)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config_module)
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_BUCKET", "archive")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_REGION", "us-test-1")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_CONNECT_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_READ_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_OPERATION_TIMEOUT_SECONDS", "11")

    provider = S3ArchiveProvider.from_env()

    assert captured["service"] == "s3"
    assert captured["client"]["endpoint_url"] == "https://s3.example"
    assert captured["client"]["region_name"] == "us-test-1"
    assert captured["config"] == {
        "connect_timeout": 2.5,
        "read_timeout": 7.5,
        "retries": {"max_attempts": 0, "mode": "standard"},
        "max_pool_connections": 4,
    }
    assert provider.operation_timeout_seconds == 11
    assert provider.account_id == "123456789012"
    assert provider.readiness(force=True)["ready"] is True


def test_s3_readiness_is_verified_cached_and_failure_is_not_ready() -> None:
    client = _S3Client(ready=True)
    provider = S3ArchiveProvider(client, bucket="archive", readiness_ttl_seconds=60)
    assert provider.readiness()["ready"] is True
    assert provider.readiness()["ready"] is True
    assert client.head_bucket_calls == 1

    client.ready = False
    failed = provider.readiness(force=True)
    assert failed["ready"] is False
    assert failed["error"] == "OSError"
    # Readiness uses the provider's bounded retry contract (three attempts by
    # default), but never reports ready after the failed probe.
    assert client.head_bucket_calls == 4


def test_s3_absolute_deadline_returns_while_blocking_sdk_call_is_quarantined() -> None:
    client = _S3Client(delay=0.2)
    provider = S3ArchiveProvider(
        client,
        bucket="archive",
        attempts=3,
        operation_timeout_seconds=0.02,
        circuit_failure_threshold=1,
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        provider.put("cold/state.bin", b"state")
    assert time.monotonic() - started < 0.15

    # A timed-out PUT has unknown remote outcome, so the circuit prevents an
    # overlapping retry while the quarantined daemon call may still finish.
    with pytest.raises(Exception, match=r"circuit|readiness probe"):
        provider.put("cold/state.bin", b"state")


def test_s3_large_put_uses_one_bounded_sdk_call() -> None:
    client = _TransferThreadTrapS3Client()
    provider = S3ArchiveProvider(client, bucket="archive", attempts=1)

    payload = b"x" * (8 * 1024 * 1024)
    digest = provider.put("cold/large.bin", payload)

    assert client.put_object_calls == 1
    assert client.objects[("archive", "cold/large.bin")] == payload
    assert digest == hashlib.sha256(payload).hexdigest()


def test_s3_half_open_readiness_ignores_stale_failure_cache() -> None:
    client = _S3Client(ready=False)
    provider = S3ArchiveProvider(
        client,
        bucket="archive",
        attempts=1,
        readiness_ttl_seconds=60,
        circuit_failure_threshold=1,
        circuit_cooldown_seconds=1,
    )
    assert provider.readiness(force=True)["ready"] is False
    with provider._state_lock:
        provider._readiness_checked_at = time.monotonic() - 61
    assert provider.readiness()["ready"] is False
    assert client.head_bucket_calls == 1

    client.ready = True
    with provider._state_lock:
        provider._circuit_open_until = time.monotonic() - 1

    recovered = provider.readiness()
    assert recovered["ready"] is True
    assert client.head_bucket_calls == 2
    provider.put("cold/recovered.bin", b"recovered")


def test_s3_get_timeout_schedules_bounded_body_close() -> None:
    body = _BlockingS3Body()
    provider = S3ArchiveProvider(
        _BlockingBodyS3Client(body),
        bucket="archive",
        attempts=1,
        operation_timeout_seconds=0.02,
        circuit_failure_threshold=1,
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        provider.get("cold/blocked.bin")
    assert time.monotonic() - started < 0.15
    assert body.read_started.is_set()
    assert body.closed.wait(0.2)


def test_s3_get_rejects_a_body_that_ignores_the_read_bound() -> None:
    body = _OversizedS3Body()
    provider = S3ArchiveProvider(
        _OversizedBodyS3Client(body),
        bucket="archive",
        attempts=1,
    )

    with pytest.raises(Exception, match="configured read limit"):
        provider.get("cold/oversized.bin", max_bytes=32)
    assert body.closed is True
