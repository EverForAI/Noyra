from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.service_contract import route_contracts
from noyra.world import FetchedDocument, ObservationStore, SafeWebReader, SourceRegistry
from noyra.world.diagnostics import SOURCE_LIMIT, AcquisitionQueryLimit, WorldAcquisitionProjection
from noyra.world.feed import feed_content, parse_feed

TOKEN = "synthetic-world-operator-token-long-enough"
READ_TOKEN = "synthetic-world-read-token-long-enough"


def test_projection_uses_poll_state_not_observation_count_and_is_read_only(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    IdentityStore(database).ensure("one", content_hash("one"))
    IdentityStore(database).ensure("two", content_hash("two"))
    registry = SourceRegistry(database)
    source = registry.register("one", "News", "https://example.com/rss", "rss", status="active")
    registry.register("two", "Other subject", "https://example.org/rss", "rss", status="active")
    title, entries = parse_feed(
        "<rss><channel><item><guid>1</guid><title>New evidence</title></item></channel></rss>",
        source.url,
    )
    content = feed_content(entries)
    store = ObservationStore(database)
    for minute in ("00", "15"):
        document = FetchedDocument(
            source.url,
            title,
            content,
            content_hash(content),
            "application/rss+xml",
            (),
            None,
            None,
            f"2026-08-11T00:{minute}:00+00:00",
            entries,
        )
        store.ingest("one", source.source_id, document)
    with database.connection() as connection:
        events_before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    projection = WorldAcquisitionProjection(database).summary("one")
    assert projection["scope"] == "since_tracking_started"
    assert len(projection["items"]) == 1
    item = projection["items"][0]
    assert item["checks"] == 2 and item["unchanged_checks"] == 1
    assert item["outcome"] == "unchanged" and item["new_item_versions"] == 1
    assert item["last_success_at"] == "2026-08-11T00:15:00+00:00"
    assert item["last_change_at"] == "2026-08-11T00:00:00+00:00"
    assert "New evidence" not in json.dumps(projection)
    assert "Other subject" not in json.dumps(projection)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_projection_caps_sources_and_marks_untracked_state_unknown(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    IdentityStore(database).ensure("one", content_hash("one"))
    registry = SourceRegistry(database)
    for number in range(SOURCE_LIMIT + 1):
        registry.register(
            "one", f"Source {number}", f"https://example.com/{number}", "web", status="active"
        )
    projection = WorldAcquisitionProjection(database).summary("one")
    assert projection["truncated"] is True
    assert len(projection["items"]) == SOURCE_LIMIT
    assert all(item["checks"] is None and item["outcome"] is None for item in projection["items"])
    assert all(item["tracking"] == "uninitialized" for item in projection["items"])
    with (
        patch("noyra.world.diagnostics.QUERY_BUDGET_SECONDS", 0),
        pytest.raises(AcquisitionQueryLimit),
    ):
        WorldAcquisitionProjection(database).summary("one")


def test_admin_acquisition_route_requires_operator_and_never_fetches(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "test.sqlite3", "acquisition-http", content_hash("http"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=kernel.subject_id,
        genesis_hash=content_hash("http"),
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr(TOKEN),
        read_token=SecretStr(READ_TOKEN),
    )
    server = NoyraHTTPServer(kernel, settings)
    try:
        server.start()
        url = f"http://127.0.0.1:{server.address[1]}/api/v1/admin/world-acquisition"
        for headers in ({}, {"Authorization": f"Bearer {READ_TOKEN}"}):
            with pytest.raises(HTTPError) as denied:
                urlopen(Request(url, headers=headers), timeout=5)
            assert denied.value.code == 401
            denied.value.close()
        headers = {"Authorization": f"Bearer {TOKEN}"}
        with (
            patch.object(SafeWebReader, "fetch", side_effect=AssertionError("must not fetch")),
            patch.object(server, "diagnostics", side_effect=AssertionError("must not full-scan")),
        ):
            with urlopen(Request(url, headers=headers), timeout=5) as response:
                body: dict[str, Any] = json.load(response)
            assert body["format"] == "noyra-world-acquisition/v1"
            assert TOKEN not in json.dumps(body) and READ_TOKEN not in json.dumps(body)
            with urlopen(
                Request(url.replace("/api/v1/", "/api/"), headers=headers), timeout=5
            ) as alias:
                assert json.load(alias)["format"] == body["format"]
        with patch("noyra.world.diagnostics.QUERY_BUDGET_SECONDS", 0):
            with pytest.raises(HTTPError) as unavailable:
                urlopen(Request(url, headers=headers), timeout=5)
            assert unavailable.value.code == 503
            assert json.load(unavailable.value) == {"error": "world_acquisition_unavailable"}
            unavailable.value.close()
        contract = next(
            row for row in route_contracts() if row.path == "/api/v1/admin/world-acquisition"
        )
        assert contract.role == "operator" and 503 in contract.effective_responses
    finally:
        server.close()
        kernel.close()
