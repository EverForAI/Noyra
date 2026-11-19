from __future__ import annotations

import ipaddress
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpcore
import pytest

from noyra.capability import CapabilityGrant, CapabilityIntegrity, CapabilityStore, ToolRunner
from noyra.capability.errors import CapabilityDeniedError
from noyra.core import SubjectKernel
from noyra.core.http import is_public_address
from noyra.core.types import canonical_json, content_hash
from noyra.world import SafeWebReader, SourceRegistry
from noyra.world.errors import UnsafeSourceError
from noyra.world.source import canonical_public_url

NON_PUBLIC = (
    "127.0.0.1",
    "10.0.0.1",
    "169.254.169.254",
    "0.0.0.0",
    "100.64.0.1",
    "198.18.0.1",
    "224.0.0.1",
    "239.255.255.250",
    "192.0.0.9",
    "192.88.99.1",
    "::",
    "::1",
    "fe80::1",
    "fc00::1",
    "ff02::1",
    "64:ff9b::808:808",
    "64:ff9b:1::a9fe:a9fe",
    "::ffff:8.8.8.8",
    "2002:0808:0808::",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "3fff::1",
    "5f00::1",
    "2606:4700:4700::1111%1",
    "2606:4700:4700::1111%25eth0",
)
PUBLIC = ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888")


def url_for(address: str) -> str:
    return f"https://[{address}]/article" if ":" in address else f"https://{address}/article"


def scope_for(address: str, kind: str) -> dict[str, object]:
    return {"public_https": True} if kind == "public" else {"hosts": [address]}


@pytest.mark.parametrize("kind", ["public", "host"])
@pytest.mark.parametrize("address", NON_PUBLIC)
def test_capability_literal_and_source_gate_share_public_unicast_policy(
    address: str, kind: str
) -> None:
    assert not is_public_address(ipaddress.ip_address(address))
    assert not CapabilityStore._scope_matches(
        "web_read", canonical_json(scope_for(address, kind)), url_for(address)
    )
    with pytest.raises(UnsafeSourceError):
        canonical_public_url(url_for(address))


@pytest.mark.parametrize("kind", ["public", "host"])
@pytest.mark.parametrize("address", PUBLIC)
def test_capability_still_accepts_public_ipv4_ipv6_and_standard_https_port(
    address: str, kind: str
) -> None:
    scope = canonical_json(scope_for(address, kind))
    assert is_public_address(ipaddress.ip_address(address))
    assert CapabilityStore._scope_matches("web_read", scope, url_for(address))
    assert CapabilityStore._scope_matches(
        "web_read", scope, url_for(address).replace("/article", ":443/article")
    )
    assert canonical_public_url(url_for(address)) == url_for(address)


@dataclass
class WebSetup:
    kernel: SubjectKernel
    store: CapabilityStore
    tools: ToolRunner


@pytest.fixture
def web(tmp_path: Path) -> Iterator[WebSetup]:
    kernel = SubjectKernel(tmp_path / "web.sqlite3", "m09-web", content_hash("m09-web"))
    try:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        yield WebSetup(kernel, CapabilityStore(kernel.database), ToolRunner(kernel.database))
    finally:
        kernel.close()


def grant(web: WebSetup, scope: dict[str, Any]) -> str:
    record = web.store.grant(
        web.kernel.subject_id,
        CapabilityGrant(
            capability_type="web_read",
            scope=scope,
            issuer="operator",
            rate_limit_per_hour=20,
            side_effect=False,
        ),
        actor="operator",
    )
    return record.grant_id


@pytest.mark.parametrize("kind", ["public", "host"])
def test_unsafe_literals_never_consume_uses_or_change_persisted_grants(
    web: WebSetup, kind: str
) -> None:
    scope = {"public_https": True} if kind == "public" else {"hosts": list(NON_PUBLIC)}
    grant(web, scope)
    before = web.store.list(web.kernel.subject_id)
    for address in NON_PUBLIC:
        assert not web.store.allows(
            web.kernel.subject_id, "web_read", url_for(address), side_effect=False
        )
        with pytest.raises(CapabilityDeniedError):
            web.store.use(web.kernel.subject_id, "web_read", url_for(address), side_effect=False)
    assert web.store.list(web.kernel.subject_id) == before
    counts = CapabilityIntegrity(web.kernel.database).verify(web.kernel.subject_id)
    assert counts == {"capability_grants": 1, "capability_uses": 0}


@pytest.mark.parametrize("kind", ["public", "host"])
def test_host_scope_authorization_is_local_and_does_not_claim_dns_safety(
    web: WebSetup, kind: str
) -> None:
    scope = (
        {"public_https": True} if kind == "public" else {"hosts": ["Public.Example", "localhost"]}
    )
    grant(web, scope)
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("No DNS in capability matching")),
        patch("noyra.core.http._system_address_lookup", side_effect=AssertionError("No DNS")),
    ):
        for host in ("public.example", "PUBLIC.EXAMPLE", "localhost"):
            assert web.store.allows(
                web.kernel.subject_id, "web_read", f"https://{host}/", side_effect=False
            )
        assert web.store.allows(
            web.kernel.subject_id, "web_read", "https://other.example/", side_effect=False
        ) == (kind == "public")
    assert (
        CapabilityIntegrity(web.kernel.database).verify(web.kernel.subject_id)["capability_uses"]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [["127.0.0.1"], ["::1"], ["224.0.0.1"], ["64:ff9b::808:808"], ["8.8.8.8", "10.0.0.1"]],
    ids=["loopback-v4", "loopback-v6", "multicast", "translation", "mixed-result"],
)
async def test_authorized_hostname_cannot_connect_to_non_public_dns_answers(
    web: WebSetup, monkeypatch: pytest.MonkeyPatch, answers: list[str]
) -> None:
    grant(web, {"hosts": ["callback.example"]})
    source = SourceRegistry(web.kernel.database).register(
        web.kernel.subject_id,
        "M09 source",
        "https://callback.example/article",
        "web",
        status="active",
        reason="synthetic registered source",
    )
    resolved: list[str] = []

    async def resolver(host: str, port: int) -> Sequence[str]:
        resolved.append(host)
        assert port == 443
        return answers

    connector = AsyncMock(side_effect=AssertionError("Non-public TCP must never start"))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connector)
    reader = SafeWebReader(resolver=resolver)
    try:
        result = await web.tools.fetch_document(web.kernel.subject_id, source, reader)
    finally:
        await reader.aclose()
    assert result.status == "failed" and result.document is None
    assert resolved == ["callback.example"]
    connector.assert_not_called()
    assert (
        CapabilityIntegrity(web.kernel.database).verify(web.kernel.subject_id)["capability_uses"]
        == 1
    )
    web.tools.actions.verify_integrity(web.kernel.subject_id)


class ResponseStream(httpcore.AsyncNetworkStream):
    def __init__(self, address: str, sent: list[bytes], tls: list[str | None]):
        self.address = address
        self.sent = sent
        self.tls = tls
        self.remaining = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 15\r\n"
            b"Connection: close\r\n\r\nM09 public text"
        )

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data, self.remaining = self.remaining[:max_bytes], self.remaining[max_bytes:]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.sent.append(buffer)

    async def aclose(self) -> None:
        pass

    async def start_tls(self, *args: Any, **kwargs: Any) -> ResponseStream:
        self.tls.append(kwargs.get("server_hostname"))
        return self

    def get_extra_info(self, info: str) -> Any:
        return (self.address, 443) if info == "server_addr" else None


@pytest.mark.asyncio
async def test_each_new_connection_rechecks_dns_and_pins_ip_with_original_tls_host(
    web: WebSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant(web, {"public_https": True})
    source = SourceRegistry(web.kernel.database).register(
        web.kernel.subject_id,
        "M09 source",
        "https://public.example/article",
        "web",
        status="active",
        reason="synthetic registered source",
    )
    resolved: list[str] = []
    connected: list[str] = []
    sent: list[bytes] = []
    tls: list[str | None] = []

    async def resolver(host: str, port: int) -> Sequence[str]:
        resolved.append(host)
        return ["8.8.8.8"] if len(resolved) == 1 else ["127.0.0.1"]

    async def connect(self: Any, host: str, *args: Any, **kwargs: Any) -> ResponseStream:
        connected.append(host)
        assert host == "8.8.8.8"
        return ResponseStream(host, sent, tls)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    reader = SafeWebReader(resolver=resolver)
    try:
        first = await web.tools.fetch_document(
            web.kernel.subject_id, source, reader, idempotency_key="m09-first"
        )
        assert first.status == "succeeded" and first.document is not None
        assert first.document.content == "M09 public text"
        second = await web.tools.fetch_document(
            web.kernel.subject_id, source, reader, idempotency_key="m09-dns-changed"
        )
        assert second.status == "failed" and second.document is None
    finally:
        await reader.aclose()
    assert resolved == ["public.example", "public.example"]
    assert connected == ["8.8.8.8"] and tls == ["public.example"]
    assert sum(chunk.startswith(b"GET ") for chunk in sent) == 1
    assert (
        CapabilityIntegrity(web.kernel.database).verify(web.kernel.subject_id)["capability_uses"]
        == 2
    )
    web.tools.actions.verify_integrity(web.kernel.subject_id)
