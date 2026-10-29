from __future__ import annotations

import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.http import (
    PublicDNSAsyncHTTPTransport,
    PublicDNSHTTPTransport,
    PublicEndpointRejected,
)
from noyra.core.types import content_hash
from noyra.knowledge import CommonKnowledgePeerInput
from noyra.knowledge.sync import CommonKnowledgeHTTPClient
from noyra.model import (
    BudgetLimits,
    ModelGateway,
    ModelLedger,
    ModelMessage,
    ModelPricing,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    RetryPolicy,
)
from noyra.model.embedding import EmbeddingSettings, OpenAIEmbeddingProvider
from noyra.model.errors import EmbeddingProviderError, ProviderCallError
from test_m42_p3_02_common_knowledge_sync import _PublisherClient, _store

CASES = ("private", "empty", "invalid", "dns", "dns_timeout", "connect", "connect_timeout")


def resolve(case: str) -> Sequence[str]:
    if case == "dns":
        raise socket.gaierror("synthetic DNS failure")
    if case == "dns_timeout":
        raise TimeoutError("synthetic DNS timeout")
    return {
        "private": ["127.0.0.1"],
        "empty": [],
        "invalid": ["synthetic-invalid-address"],
    }.get(case, ["8.8.8.8", "1.1.1.1"])


def expected_error(case: str) -> type[httpx.HTTPError]:
    return httpx.ConnectTimeout if case.endswith("timeout") else httpx.ConnectError


@pytest.mark.parametrize("case", CASES)
def test_sync_preconnect_errors_obey_httpx_contract(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    connects: list[str] = []

    def connect(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        connects.append(host)
        if case == "connect_timeout":
            raise httpcore.ConnectTimeout("synthetic connect timeout")
        raise OSError("synthetic connection failure")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with (
        httpx.Client(
            transport=PublicDNSHTTPTransport(resolver=lambda h, p: resolve(case))
        ) as client,
        pytest.raises(expected_error(case)) as caught,
    ):
        client.get("https://example.test")
    assert isinstance(caught.value.__cause__, PublicEndpointRejected) == (
        case in {"private", "empty", "invalid"}
    )
    assert len(connects) == (2 if case.startswith("connect") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_async_preconnect_errors_obey_httpx_contract(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def resolver(host: str, port: int) -> Sequence[str]:
        return resolve(case)

    connect = AsyncMock(
        side_effect=httpcore.ConnectTimeout("synthetic timeout")
        if case == "connect_timeout"
        else OSError("synthetic connection failure")
    )
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(
        transport=PublicDNSAsyncHTTPTransport(resolver=resolver)
    ) as client:
        with pytest.raises(expected_error(case)) as caught:
            await client.get("https://example.test")
    assert isinstance(caught.value.__cause__, PublicEndpointRejected) == (
        case in {"private", "empty", "invalid"}
    )
    assert connect.call_count == (2 if case.startswith("connect") else 0)


def test_default_dns_failures_and_literal_policy_rejection_are_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dns(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("synthetic DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with httpx.Client(transport=PublicDNSHTTPTransport()) as client:
        with pytest.raises(httpx.ConnectError) as failure:
            client.get("https://example.test")
        assert not isinstance(failure.value.__cause__, PublicEndpointRejected)
        with pytest.raises(httpx.ConnectError) as rejected:
            client.get("https://127.0.0.1")
        assert isinstance(rejected.value.__cause__, PublicEndpointRejected)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_multi_address_fallback_pins_tls_and_sends_once(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    connects: list[str] = []
    sent: list[bytes] = []
    tls: list[str | None] = []

    class Stream(httpcore.NetworkStream):
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            return b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            sent.append(buffer)

        def close(self) -> None:
            pass

        def start_tls(self, *args: Any, **kwargs: Any) -> Any:
            tls.append(kwargs["server_hostname"])
            return self

    class AsyncStream(httpcore.AsyncNetworkStream):
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            return Stream().read(max_bytes, timeout)

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            sent.append(buffer)

        async def aclose(self) -> None:
            pass

        async def start_tls(self, *args: Any, **kwargs: Any) -> Any:
            tls.append(kwargs["server_hostname"])
            return self

    def connect(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        connects.append(host)
        if len(connects) == 1:
            raise OSError("synthetic first address failure")
        return Stream()

    async def async_connect(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        connect(self, host)
        return AsyncStream()

    async def resolver(host: str, port: int) -> Sequence[str]:
        return ["8.8.8.8", "1.1.1.1"]

    if asynchronous:
        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", async_connect)
        async with httpx.AsyncClient(
            transport=PublicDNSAsyncHTTPTransport(resolver=resolver)
        ) as client:
            assert (await client.get("https://example.test")).status_code == 200
    else:
        monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
        with httpx.Client(
            transport=PublicDNSHTTPTransport(resolver=lambda h, p: ["8.8.8.8", "1.1.1.1"])
        ) as sync_client:
            assert sync_client.get("https://example.test").status_code == 200
    assert connects == ["8.8.8.8", "1.1.1.1"]
    assert tls == ["example.test"]
    assert sum(buffer.startswith(b"GET ") for buffer in sent) == 1


class Answer(BaseModel):
    result: str


def gateway(db: Database, client: httpx.AsyncClient) -> ModelGateway:
    return ModelGateway(
        OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://example.test/v1",
                model="synthetic",
                api_key=SecretStr("synthetic"),
            ),
            client=client,
        ),
        ModelLedger(db),
        model="synthetic",
        limits=BudgetLimits(
            daily_attempts=10,
            daily_input_tokens=10000,
            daily_output_tokens=10000,
            daily_cost_microusd=1000000,
        ),
        pricing=ModelPricing(
            input_microusd_per_million=1000000, output_microusd_per_million=1000000
        ),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_model_unsent_failures_release_budget_and_only_transient_errors_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    resolves: list[int] = []

    async def resolver(host: str, port: int) -> Sequence[str]:
        resolves.append(1)
        return resolve(case)

    connect = AsyncMock(
        side_effect=httpcore.ConnectTimeout("synthetic timeout")
        if case == "connect_timeout"
        else OSError("synthetic connection failure")
    )
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(
        transport=PublicDNSAsyncHTTPTransport(resolver=resolver)
    ) as client:
        with pytest.raises(ProviderCallError) as caught:
            await gateway(db, client).complete_structured(
                "owner",
                "test",
                [ModelMessage(role="user", content="synthetic")],
                Answer,
                idempotency_key="failure",
                max_output_tokens=16,
            )
    assert caught.value.code == (
        "provider_endpoint_rejected"
        if case in {"private", "empty", "invalid"}
        else "provider_connect_failed"
    )
    assert not caught.value.outcome_unknown
    assert len(resolves) == (1 if case in {"private", "empty", "invalid"} else 2)
    assert connect.call_count == (4 if case.startswith("connect") else 0)
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
        attempts = connection.execute("SELECT * FROM model_attempts").fetchall()
    assert len(attempts) == len(resolves)
    assert all(row["status"] == "failed" and row["cost_microusd"] == 0 for row in attempts)
    assert all(row["input_tokens"] == row["output_tokens"] == 0 for row in attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "write", "raw"])
async def test_postsend_and_unclassified_errors_keep_unknown_without_retry(
    tmp_path: Path, kind: str
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    sends: list[int] = []

    def request(req: httpx.Request) -> httpx.Response:
        sends.append(1)
        if kind == "read":
            raise httpx.ReadError("synthetic", request=req)
        if kind == "write":
            raise httpx.WriteError("synthetic", request=req)
        raise OSError("unclassified custom provider failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(ProviderCallError) as caught:
            await gateway(db, client).complete_structured(
                "owner",
                "test",
                [ModelMessage(role="user", content="synthetic")],
                Answer,
                idempotency_key="unknown",
                max_output_tokens=16,
            )
    assert caught.value.outcome_unknown
    assert sends == [1]
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "unknown"
        row = connection.execute("SELECT * FROM model_attempts").fetchone()
    assert row["status"] == "unknown" and row["cost_microusd"] is None
    assert row["reserved_cost_microusd"] > 0


@pytest.mark.parametrize("case", CASES)
def test_embedding_preconnect_failure_is_known_unsent(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        if case == "connect_timeout":
            raise httpcore.ConnectTimeout("synthetic timeout")
        raise OSError("synthetic connection failure")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with PublicDNSHTTPTransport(resolver=lambda h, p: resolve(case)) as transport:
        provider = OpenAIEmbeddingProvider(
            EmbeddingSettings(
                base_url="https://example.test/v1",
                model="synthetic",
                api_key=SecretStr("synthetic"),
            ),
            transport=transport,
        )
        with pytest.raises(EmbeddingProviderError) as caught:
            provider.embed(["synthetic"])
    assert not caught.value.usage_unknown


@pytest.mark.parametrize("case", ["dns", "dns_timeout", "private"])
def test_common_preconnect_failure_is_recorded_and_does_not_starve_other_peers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    pk, publisher = _store(tmp_path, "publisher")
    bk, bad_publisher = _store(tmp_path, "bad-publisher")
    rk, reader = _store(tmp_path, "reader")
    peers = [
        reader.register_peer(
            CommonKnowledgePeerInput(
                label=label,
                endpoint=f"https://{label}.example.test",
                publisher_subject_id=node.subject_id,
                public_key=node.public_key,
            ),
            actor="operator",
        )
        for label, node in [("bad", bad_publisher), ("good", publisher)]
    ]

    class Healthy(_PublisherClient):
        def close(self) -> None:
            pass

    clients: list[httpx.Client] = []

    def client(endpoint: str, **kwargs: Any) -> Any:
        if "bad." in endpoint:
            http = httpx.Client(
                transport=PublicDNSHTTPTransport(resolver=lambda h, p: resolve(case))
            )
            clients.append(http)
            return CommonKnowledgeHTTPClient(endpoint, client=http)
        return Healthy(publisher)

    monkeypatch.setattr("noyra.knowledge.common.CommonKnowledgeHTTPClient", client)
    try:
        assert reader.sync_due(max_peers=2) == 1
        assert reader.peer(peers[0].peer_id).last_error_code == (
            "common_knowledge_peer_timeout"
            if case == "dns_timeout"
            else "common_knowledge_peer_unavailable"
        )
        assert reader.peer(peers[0].peer_id).next_sync_at > peers[0].next_sync_at
        with rk.database.connection() as connection:
            outcomes = connection.execute(
                "SELECT outcome FROM common_knowledge_sync_runs ORDER BY rowid"
            ).fetchall()
        assert [row[0] for row in outcomes] == ["failed", "succeeded"]
    finally:
        for http in clients:
            http.close()
        rk.close()
        bk.close()
        pk.close()


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::808:808",
        "64:ff9b:1::a9fe:a9fe",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "224.0.0.1",
        "239.255.255.250",
        "ff02::1",
        "::ffff:8.8.8.8",
        "192.0.0.8",
        "192.88.99.1",
        "100:0:0:1::1",
        "3fff::1",
        "5f00::1",
        "2606:4700:4700::1111%1",
    ],
)
def test_public_endpoint_rejects_conversion_multicast_and_special_use(address: str) -> None:
    from noyra.core.http import PublicAddressRejected, public_addresses, validate_public_addresses
    from noyra.world.source import UnsafeSourceError, canonical_public_url

    with pytest.raises(PublicAddressRejected):
        public_addresses(address, 443)
    with pytest.raises(PublicAddressRejected):
        validate_public_addresses(["8.8.8.8", address])
    literal = f"[{address}]" if ":" in address else address
    with pytest.raises((UnsafeSourceError, ValueError)):
        canonical_public_url(f"https://{literal}/")


def test_public_unicast_ipv4_ipv6_remain_usable() -> None:
    from noyra.core.http import public_addresses, validate_public_addresses

    for address in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888"):
        assert public_addresses(address, 443) == [address]
        assert validate_public_addresses([address, address]) == [address]
