from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.research.browser import BrowserSearchExecutor
from noyra.research.routing import set_search_routing_mode
from test_external_dispatch_fencing import database, fence


def options(key: str = "browser") -> dict[str, Any]:
    return dict(
        goal_id=None,
        strategy_id=None,
        expected_outcome="synthetic",
        idempotency_key=key,
        limit=2,
        hourly_limit=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pause", "quarantine", "drain", "migration", "routing"])
async def test_browser_final_fence_blocks_stale_query_and_settles_action(
    tmp_path: Path, kind: str
) -> None:
    db, subject, gate = database(tmp_path)
    sends = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if kind == "routing":
                set_search_routing_mode(db, subject, "api_first")
            else:
                fence(gate, kind)
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            sends.append(request)
            raise AssertionError("stale browser query was sent")

    async with httpx.AsyncClient(transport=Transport()) as client:
        browser = BrowserSearchExecutor(db, client=client)
        with gate.operation("test") as lease, bind_lease(lease):
            if kind == "routing":
                result = await browser.search(subject, "synthetic", **options())
                assert result.status == "failed"
            else:
                with pytest.raises(OperationInvalidated):
                    await browser.search(subject, "synthetic", **options())
        assert sends == []
        with db.connection() as connection:
            assert connection.execute("SELECT status FROM actions").fetchone()[0] == "failed"
        replay = await browser.search(subject, "synthetic", **options())
        assert replay.status == "failed" and sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown", "error", "cancel"])
async def test_browser_sent_outcome_settles_then_refuses_stale_result(
    tmp_path: Path, outcome: str
) -> None:
    db, subject, gate = database(tmp_path)

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            gate.invalidate()
            if outcome == "unknown":
                raise httpx.ReadTimeout("synthetic")
            if outcome == "cancel":
                raise asyncio.CancelledError
            return httpx.Response(
                503 if outcome == "error" else 200, content=b"<rss><channel/></rss>"
            )

    async with httpx.AsyncClient(transport=Transport()) as client:
        browser = BrowserSearchExecutor(db, client=client)
        with (
            gate.operation("test") as lease,
            bind_lease(lease),
            pytest.raises(asyncio.CancelledError if outcome == "cancel" else OperationInvalidated),
        ):
            await browser.search(subject, "synthetic", **options())
        with db.connection() as connection:
            assert (
                connection.execute("SELECT status FROM actions").fetchone()[0]
                == {
                    "success": "succeeded",
                    "unknown": "unknown",
                    "error": "failed",
                    "cancel": "unknown",
                }[outcome]
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM browser_search_reservations").fetchone()[0]
                == 1
            )


@pytest.mark.asyncio
async def test_browser_initial_fence_and_idempotent_budget(tmp_path: Path) -> None:
    db, subject, gate = database(tmp_path)
    sends = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(200, content=b"<rss><channel/></rss>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = BrowserSearchExecutor(db, client=client)
        with gate.operation("test") as lease, bind_lease(lease):
            gate.invalidate()
            with pytest.raises(OperationInvalidated):
                await browser.search(subject, "synthetic", **options())
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        result = await browser.search(subject, "synthetic", **options())
        replay = await browser.search(subject, "synthetic", **options())
        assert replay.action_id == result.action_id and len(sends) == 1
        with pytest.raises(PermissionError, match="hourly limit"):
            await browser.search(subject, "different", **options("second"))
        assert len(sends) == 1
