from __future__ import annotations

import asyncio
import subprocess
from typing import Any
from urllib.parse import urlencode

import httpcore
import pytest

import noyra.core.http as network
import test_world_feed_poll as feed_helpers
from noyra.core.types import content_hash
from noyra.world import SafeWebReader
from noyra.world.errors import FetchError, UnsafeSourceError
from noyra.world.source import canonical_public_url
from noyra.world.store import ObservationStore
from test_world_feed_poll import SUBJECT, T1, document, item, rss

context = feed_helpers.context


@pytest.mark.parametrize(
    "name",
    [
        "author",
        "keyword",
        "monkey",
        "keynote",
        "topic",
        "page",
        "search",
        "author_id",
    ],
)
def test_public_query_parameter_names_are_not_substring_blocked(name: str) -> None:
    url = "https://example.com/article?" + urlencode({name: "public"})
    assert canonical_public_url(url) == url


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "access_token",
        "accessToken",
        "KEY",
        "client-secret",
        "password",
        "Signature",
        "x-amz-signature",
        "Authorization",
        "session_id",
        "oauth_token",
        "sig",
    ],
)
def test_credential_query_names_still_require_private_transport(name: str) -> None:
    with pytest.raises(UnsafeSourceError):
        canonical_public_url("https://example.com/?" + urlencode({name: "synthetic-secret"}))


def test_encoded_and_repeated_credential_names_cannot_hide() -> None:
    for query in ("author=one&%61pi%5Fkey=synthetic", "page=1&token=&page=2", "key=one&key=two"):
        with pytest.raises(UnsafeSourceError):
            canonical_public_url("https://example.com/?" + query)


@pytest.mark.asyncio
async def test_default_dns_receives_remaining_connect_budget_after_gate_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = []

    async def delayed_worker(function: Any, *args: Any, timeout: Any) -> Any:
        await asyncio.sleep(0.04)
        return function(*args)

    def resolve(host: str, port: int, *, timeout: float) -> list[str]:
        received.append(timeout)
        return ["93.184.216.34"]

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise httpcore.ConnectError("synthetic connector")

    monkeypatch.setattr(network, "_call_sync_resolver_async", delayed_worker)
    monkeypatch.setattr(network, "public_addresses", resolve)
    backend = network._PublicDNSAsyncBackend()
    monkeypatch.setattr(backend._backend, "connect_tcp", unavailable)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("fixture.test", 443, timeout=0.5)
    assert len(received) == 1 and 0 < received[0] < 0.48


@pytest.mark.asyncio
async def test_reader_short_deadline_reaps_stuck_dns_not_fixed_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.world.types import SourceRecord

    children: list[Any] = []
    real_spawn = subprocess.Popen

    def spawn(*args: Any, **kwargs: Any) -> Any:
        child = real_spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(network, "_DNS_LOOKUP_CODE", "import time; time.sleep(60)")
    source = SourceRecord(
        "source",
        "subject",
        "fixture",
        "https://fixture.test/feed",
        "rss",
        0.5,
        "active",
        content_hash("fixture"),
        1,
        T1,
        T1,
    )
    reader = SafeWebReader(total_timeout_seconds=0.15)
    try:
        with pytest.raises(FetchError):
            await reader.fetch(source)
        for _ in range(200):
            if children and all(child.poll() is not None for child in children):
                break
            await asyncio.sleep(0.01)
        assert children and all(child.poll() is not None for child in children)
    finally:
        await reader.aclose()


def test_seen_lookup_cost_depends_on_current_identities_not_retained_history(context: Any) -> None:
    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    with database.transaction() as c:
        c.executemany(
            "INSERT INTO world_feed_seen VALUES (?,?,?,?,?,?)",
            [
                (source.source_id, SUBJECT, 1, content_hash({"key": n}), content_hash({"v": n}), T1)
                for n in range(16_000)
            ],
        )
    with database.connection() as c:
        wanted = {(content_hash({"key": 23}), content_hash({"v": 23}))}
        work = [0]
        c.set_progress_handler(lambda: work.__setitem__(0, work[0] + 1) or 0, 100)
        result = ObservationStore._seen_times(c, SUBJECT, source.source_id, wanted)
        current_work = work[0]
        work[0] = 0
        c.execute(
            "SELECT item_key,fingerprint,first_seen_at FROM world_feed_seen "
            "WHERE source_id=? AND subject_id=? AND fingerprint_version=1 LIMIT 16384",
            (source.source_id, SUBJECT),
        ).fetchall()
        historical_work = work[0]
        c.set_progress_handler(None, 0)
    assert set(result) == wanted
    assert historical_work > 100 and current_work < historical_work / 10
