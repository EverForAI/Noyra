from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

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

from .provider import SearchProviderStore
from .types import SearchExecution, SearchProviderRecord, SearchResult

SEARCH_ENDPOINTS = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "bing": "https://api.bing.microsoft.com/v7.0/search",
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
}
MAX_SEARCH_RESULTS = 20


class SearchOutcomeUnknownError(RuntimeError):
    pass


class SearchExecutor:
    """Audited adapters for user-provided search APIs."""

    def __init__(
        self,
        database: Database,
        providers: SearchProviderStore,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.providers = providers
        self.actions = ActionLedger(database)
        self.clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=8),
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSAsyncHTTPTransport(max_connections=8),
        )

    async def search(
        self,
        subject_id: str,
        config: SearchProviderRecord,
        query: str,
        *,
        goal_id: str,
        project_id: str | None = None,
        phase_id: str | None = None,
        strategy_id: str,
        expected_outcome: str,
        idempotency_key: str,
        limit: int = 8,
    ) -> SearchExecution:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 512:
            raise ValueError("search query is invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError("search result limit is invalid")
        if config.subject_id != subject_id or config.status != "active":
            raise PermissionError("search provider is unavailable")
        query_hash = content_hash(normalized_query)
        action_resource = {
            "config_id": config.config_id,
            "query_hash": query_hash,
            "limit": limit,
        }
        action = self.actions.prepare(
            subject_id,
            "search",
            f"search_api:{config.provider_type}",
            "[private-search-query]",
            action_resource,
            goal_id=goal_id,
            project_id=project_id,
            phase_id=phase_id,
            strategy_id=strategy_id,
            expected_outcome=expected_outcome,
            side_effect=False,
            resource_cost=action_resource,
            idempotency_key=idempotency_key,
        )
        if action.status == "succeeded":
            return SearchExecution(
                action.action_id,
                config.config_id,
                config.provider_type,
                normalized_query,
                self._results_from_action(action.result),
            )
        if action.status in {"failed", "cancelled", "unknown"}:
            return SearchExecution(
                action.action_id,
                config.config_id,
                config.provider_type,
                normalized_query,
                (),
            )
        try:
            self._reserve_use(config, action.action_id, normalized_query)
        except Exception:
            if action.status == "prepared":
                self.actions.cancel(action.action_id, "search resource authorization failed")
            raise
        if action.status != "prepared":
            raise RuntimeError(f"search action cannot resume from state {action.status}")
        action = self.actions.start(action.action_id)
        try:
            response = await self._request(config, normalized_query, limit)
            results = self._parse(config.provider_type, response, limit)
        except asyncio.CancelledError:
            self.actions.finish(
                action.action_id,
                "unknown",
                {"error": "search_cancelled", "outcome_unknown": True},
                public_goal_reference=goal_id,
                public_explanation=(
                    f"Configured {config.provider_type} search was interrupted; outcome is unknown."
                ),
                resource_summary="search completion unknown after interruption",
            )
            raise
        except SearchOutcomeUnknownError as error:
            self.actions.finish(
                action.action_id,
                "unknown",
                {"error": type(error).__name__, "outcome_unknown": True},
                public_goal_reference=goal_id,
                public_explanation=(
                    f"Configured {config.provider_type} search ended with an unknown outcome."
                ),
                resource_summary="search completion unknown after transport failure",
            )
            return SearchExecution(
                action.action_id,
                config.config_id,
                config.provider_type,
                normalized_query,
                (),
            )
        except Exception as error:
            self.actions.finish(
                action.action_id,
                "failed",
                {"error": type(error).__name__},
                public_goal_reference=goal_id,
                public_explanation=(
                    f"Search with configured {config.provider_type} resource failed."
                ),
                resource_summary="search request failed",
            )
            return SearchExecution(
                action.action_id,
                config.config_id,
                config.provider_type,
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
            public_explanation=(
                f"Used the configured {config.provider_type} search resource "
                "for autonomous research."
            ),
            resource_summary=f"{len(results)} search results",
            redaction_reason="search query withheld as private research context",
        )
        return SearchExecution(
            action.action_id,
            config.config_id,
            config.provider_type,
            normalized_query,
            results,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _reserve_use(self, config: SearchProviderRecord, action_id: str, query: str) -> None:
        now = self._parse_time(self.clock())
        cutoff = (now - timedelta(hours=1)).isoformat(timespec="milliseconds")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status, rate_limit_per_hour FROM search_provider_configs "
                "WHERE config_id = ? AND subject_id = ?",
                (config.config_id, config.subject_id),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise PermissionError("search provider is no longer active")
            used = int(
                connection.execute(
                    "SELECT COUNT(*) FROM search_provider_uses WHERE config_id = ? "
                    "AND created_at > ?",
                    (config.config_id, cutoff),
                ).fetchone()[0]
            )
            if used >= int(row["rate_limit_per_hour"]):
                raise PermissionError("search provider hourly limit reached")
            connection.execute(
                """INSERT INTO search_provider_uses(
                    use_id, config_id, subject_id, action_id, query_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    new_id("searchuse"),
                    config.config_id,
                    config.subject_id,
                    action_id,
                    content_hash(query),
                    self.clock(),
                ),
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
                continue
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

    async def _request(
        self, config: SearchProviderRecord, query: str, limit: int
    ) -> httpx.Response:
        key = self.providers.api_key(config.config_id, subject_id=config.subject_id)
        endpoint = SEARCH_ENDPOINTS[config.provider_type]
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Noyra-Research/0.1.0",
        }
        if config.provider_type == "brave":
            headers["X-Subscription-Token"] = key
            method = "GET"
            url = f"{endpoint}?q={quote_plus(query)}&count={limit}"
            request_kwargs: dict[str, Any] = {"headers": headers}
        elif config.provider_type == "bing":
            headers["Ocp-Apim-Subscription-Key"] = key
            method = "GET"
            url = f"{endpoint}?q={quote_plus(query)}&count={limit}"
            request_kwargs = {"headers": headers}
        elif config.provider_type == "tavily":
            method = "POST"
            url = endpoint
            request_kwargs = {
                "headers": {**headers, "Content-Type": "application/json"},
                "json": {"api_key": key, "query": query, "max_results": limit},
            }
        else:
            headers["X-API-KEY"] = key
            method = "POST"
            url = endpoint
            request_kwargs = {
                "headers": {**headers, "Content-Type": "application/json"},
                "json": {"q": query, "num": limit},
            }
        deadline = asyncio.get_running_loop().time() + DEFAULT_TOTAL_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(DEFAULT_TOTAL_TIMEOUT_SECONDS):
                async with self._client.stream(
                    method,
                    url,
                    follow_redirects=False,
                    **request_kwargs,
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
            raise SearchOutcomeUnknownError("search provider outcome is unknown") from error
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise RuntimeError("search provider connection failed") from error
        except (
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.WriteTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as error:
            raise SearchOutcomeUnknownError("search provider outcome is unknown") from error

    @staticmethod
    def _parse(
        provider_type: str, response: httpx.Response, limit: int
    ) -> tuple[SearchResult, ...]:
        try:
            payload = response.json()
        except (ValueError, RecursionError) as error:
            raise ValueError("search provider JSON is invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("search provider payload is invalid")
        raw_results: Any
        if provider_type == "brave":
            raw_results = payload.get("web", {}).get("results", [])
        elif provider_type == "bing":
            raw_results = payload.get("webPages", {}).get("value", [])
        elif provider_type == "tavily":
            raw_results = payload.get("results", [])
        else:
            raw_results = payload.get("organic", [])
        if not isinstance(raw_results, list):
            raise ValueError("search provider results are invalid")
        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url") or item.get("link")
            if not isinstance(raw_url, str):
                continue
            try:
                url = canonical_public_url(raw_url)
            except ValueError:
                continue
            if url in seen:
                continue
            title = item.get("title") or item.get("name") or url
            snippet = item.get("description") or item.get("snippet") or item.get("content") or ""
            results.append(
                SearchResult(
                    str(title)[:512],
                    url,
                    str(snippet)[:2_000],
                    len(results) + 1,
                )
            )
            seen.add(url)
            if len(results) >= limit:
                break
        return tuple(results)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("search time requires a timezone")
        return parsed.astimezone(UTC)
