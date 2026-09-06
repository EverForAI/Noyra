from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from noyra.core.actions import ActionLedger
from noyra.core.database import Database
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    PublicDNSAsyncHTTPTransport,
    read_bounded_response,
    validate_response_headers,
)
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.world import canonical_public_url

from .types import SearchExecution, SearchResult

BROWSER_SEARCH_ENDPOINT = "https://www.bing.com/search"
BROWSER_SEARCH_TOOL = "browser_search:bing_rss"
FORBIDDEN_XML_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


class BrowserSearchOutcomeUnknownError(RuntimeError):
    pass


class BrowserSearchExecutor:
    """Lightweight public search-page adapter that does not consume a model call or API key."""

    def __init__(
        self,
        database: Database,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.actions = ActionLedger(database)
        self.clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=4),
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSAsyncHTTPTransport(max_connections=4),
        )

    async def search(
        self,
        subject_id: str,
        query: str,
        *,
        goal_id: str,
        project_id: str | None = None,
        phase_id: str | None = None,
        strategy_id: str,
        expected_outcome: str,
        idempotency_key: str,
        limit: int,
        hourly_limit: int,
    ) -> SearchExecution:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 512:
            raise ValueError("browser search query is invalid")
        action = self.actions.prepare(
            subject_id,
            "search",
            BROWSER_SEARCH_TOOL,
            "[private-search-query]",
            {"query_hash": content_hash(normalized_query), "limit": limit},
            goal_id=goal_id,
            project_id=project_id,
            phase_id=phase_id,
            strategy_id=strategy_id,
            expected_outcome=expected_outcome,
            side_effect=False,
            idempotency_key=idempotency_key,
        )
        if action.status == "succeeded":
            return SearchExecution(
                action.action_id,
                "browser:bing-rss",
                "browser",
                normalized_query,
                self._results_from_action(action.result),
            )
        if action.status in {"failed", "cancelled", "unknown"}:
            return SearchExecution(
                action.action_id,
                "browser:bing-rss",
                "browser",
                normalized_query,
                (),
            )
        if action.status != "prepared":
            raise RuntimeError(f"browser search cannot resume from state {action.status}")
        if not self._reserve(subject_id, idempotency_key, hourly_limit):
            self.actions.cancel(action.action_id, "browser search hourly limit reached")
            raise PermissionError("browser search hourly limit reached")
        action = self.actions.start(action.action_id)
        try:
            response = await self._request(normalized_query, limit)
            results = self._parse(response, limit)
        except asyncio.CancelledError:
            self._finish_unknown(action.action_id, goal_id, "browser_search_cancelled")
            raise
        except BrowserSearchOutcomeUnknownError as error:
            self._finish_unknown(action.action_id, goal_id, type(error).__name__)
            return SearchExecution(
                action.action_id,
                "browser:bing-rss",
                "browser",
                normalized_query,
                (),
            )
        except Exception as error:
            self.actions.finish(
                action.action_id,
                "failed",
                {"error": type(error).__name__},
                public_goal_reference=goal_id,
                public_explanation="Public browser search failed.",
                resource_summary="browser search request failed",
                redaction_reason="search query withheld as private research context",
            )
            return SearchExecution(
                action.action_id,
                "browser:bing-rss",
                "browser",
                normalized_query,
                (),
            )
        self.actions.finish(
            action.action_id,
            "succeeded",
            {
                "result_count": len(results),
                "result_urls": [item.url for item in results],
                "results": [item.__dict__ for item in results],
            },
            public_goal_reference=goal_id,
            public_explanation="Used a public browser-compatible search surface for research.",
            resource_summary=f"{len(results)} browser search results",
            redaction_reason="search query withheld as private research context",
        )
        return SearchExecution(
            action.action_id,
            "browser:bing-rss",
            "browser",
            normalized_query,
            results,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, query: str, limit: int) -> httpx.Response:
        deadline = asyncio.get_running_loop().time() + DEFAULT_TOTAL_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(DEFAULT_TOTAL_TIMEOUT_SECONDS):
                async with self._client.stream(
                    "GET",
                    f"{BROWSER_SEARCH_ENDPOINT}?format=rss&q={quote_plus(query)}&count={limit}",
                    headers={
                        "Accept": "application/rss+xml,application/xml,text/xml",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Mozilla/5.0 (compatible; Noyra-Research/0.1.0)",
                    },
                    follow_redirects=False,
                ) as response:
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    response.raise_for_status()
                    body = await read_bounded_response(
                        response,
                        max_body_bytes=DEFAULT_MAX_RESPONSE_BYTES,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        total_timeout_seconds=DEFAULT_TOTAL_TIMEOUT_SECONDS,
                        deadline=deadline,
                    )
                    return httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=body,
                        request=response.request,
                    )
        except TimeoutError as error:
            raise BrowserSearchOutcomeUnknownError("browser search outcome is unknown") from error
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise RuntimeError("browser search connection failed") from error
        except (
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.WriteTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as error:
            raise BrowserSearchOutcomeUnknownError("browser search outcome is unknown") from error

    @staticmethod
    def _parse(response: httpx.Response, limit: int) -> tuple[SearchResult, ...]:
        upper_content = response.content.upper()
        if any(marker in upper_content for marker in FORBIDDEN_XML_MARKERS):
            raise ValueError("browser search XML declarations are not allowed")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as error:
            raise ValueError("browser search response is invalid XML") from error
        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in root.findall(".//item"):
            raw_url = item.findtext("link")
            if raw_url is None:
                continue
            try:
                url = canonical_public_url(raw_url)
            except ValueError:
                continue
            if url in seen:
                continue
            title = (item.findtext("title") or url).strip()
            snippet = (item.findtext("description") or "").strip()
            results.append(SearchResult(title[:512], url, snippet[:2_000], len(results) + 1))
            seen.add(url)
            if len(results) >= limit:
                break
        return tuple(results)

    def _reserve(self, subject_id: str, idempotency_key: str, hourly_limit: int) -> bool:
        if hourly_limit < 0:
            raise ValueError("browser search hourly limit cannot be negative")
        now = self._parse_time(self.clock())
        cutoff = (now - timedelta(hours=1)).isoformat(timespec="milliseconds")
        reserved_at = now.isoformat(timespec="milliseconds")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM browser_search_reservations WHERE subject_id = ? "
                "AND idempotency_key = ?",
                (subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return True
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM browser_search_reservations WHERE subject_id = ? "
                    "AND reserved_at > ?",
                    (subject_id, cutoff),
                ).fetchone()[0]
            )
            if count >= hourly_limit:
                return False
            connection.execute(
                "INSERT INTO browser_search_reservations(reservation_id, subject_id, "
                "idempotency_key, reserved_at) VALUES (?, ?, ?, ?)",
                (new_id("bsearch"), subject_id, idempotency_key, reserved_at),
            )
            return True

    def _finish_unknown(self, action_id: str, goal_id: str, error_code: str) -> None:
        self.actions.finish(
            action_id,
            "unknown",
            {"error": error_code, "outcome_unknown": True},
            public_goal_reference=goal_id,
            public_explanation="Public browser search ended with an unknown outcome.",
            resource_summary="browser search completion unknown after interruption",
            redaction_reason="search query withheld as private research context",
        )

    @staticmethod
    def _results_from_action(result: dict[str, Any] | None) -> tuple[SearchResult, ...]:
        if result is None:
            return ()
        raw = result.get("results", [])
        if not isinstance(raw, list):
            return ()
        restored: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict):
                return ()
            try:
                restored.append(
                    SearchResult(
                        str(item["title"]),
                        canonical_public_url(str(item["url"])),
                        str(item["snippet"]),
                        int(item["rank"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return ()
        if canonical_json([item.__dict__ for item in restored]) != canonical_json(raw):
            return ()
        return tuple(restored)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("browser search time requires a timezone")
        return parsed.astimezone(UTC)
