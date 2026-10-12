from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlencode

import httpx

from noyra.core.admission import assert_current_lease
from noyra.core.deadline import monotonic_time
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    HTTPResponseLimitError,
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
    validate_response_headers,
)
from noyra.core.types import strict_json_loads

COMMON_KNOWLEDGE_SYNC_PROTOCOL = "noyra-common-knowledge-sync/v1"
COMMON_KNOWLEDGE_MAX_RESPONSE_BYTES = 2_000_000
COMMON_KNOWLEDGE_TOTAL_TIMEOUT_SECONDS = 10.0


class CommonKnowledgeSyncError(RuntimeError):
    """Sanitized peer synchronization failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CommonKnowledgeClientProtocol(Protocol):
    """Minimal transport surface required by the durable sync workflow."""

    def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None: ...

    def feed(self, *, cursor: int, limit: int) -> dict[str, Any]: ...


class CommonKnowledgeHTTPClient:
    """Bounded client for signed discovery and event-feed documents."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = COMMON_KNOWLEDGE_TOTAL_TIMEOUT_SECONDS,
        checkpoint: Callable[[], None] | None = None,
    ):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("common knowledge sync timeout is invalid")
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._checkpoint = checkpoint
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        # Owned clients live in their actual request worker until it exits.
        # A timed-out caller cannot close a pooled connection under that worker.
        self._closed = True

    def discovery(self, *, etag: str | None = None) -> dict[str, Any] | None:
        headers = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        return self._get("/api/common-knowledge/discovery", headers=headers, allow_304=True)

    def feed(self, *, cursor: int, limit: int) -> dict[str, Any]:
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("common knowledge feed cursor or limit is invalid")
        query = urlencode({"cursor": cursor, "limit": limit})
        result = self._get(
            f"/api/common-knowledge/feed?{query}",
            headers={"Accept": "application/json"},
            allow_304=False,
        )
        assert result is not None
        return result

    def _get(
        self,
        path: str,
        *,
        headers: dict[str, str],
        allow_304: bool,
    ) -> dict[str, Any] | None:
        timeout_hook = SyncHTTPTimeoutHook()
        deadline = monotonic_time() + self.timeout_seconds

        def remaining_budget() -> float:
            timeout_hook.assert_active()
            remaining = deadline - monotonic_time()
            if remaining <= 0 or self._closed:
                raise TimeoutError("common knowledge peer deadline exceeded")
            assert_current_lease()
            return remaining

        def active() -> float:
            remaining_budget()
            if self._checkpoint is not None:
                self._checkpoint()
            return remaining_budget()

        def trace(event: str, info: dict[str, Any]) -> None:
            if event.endswith(("send_request_headers.started", "send_request_body.started")):
                active()

        def perform_request() -> bytes | None:
            active()
            request_headers = {**headers, "Accept-Encoding": "identity"}
            context = (
                nullcontext(self._client)
                if self._client is not None
                else httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=PublicDNSHTTPTransport(max_connections=1, remaining=remaining_budget),
                )
            )
            with (
                context as client,
                client.stream(
                    "GET",
                    self.endpoint + path,
                    headers=request_headers,
                    follow_redirects=False,
                    timeout=active(),
                    extensions={"trace": trace},
                ) as response,
            ):
                timeout_hook.register(response)
                try:
                    active()
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    if allow_304 and response.status_code == 304:
                        return None
                    if response.status_code != 200:
                        raise CommonKnowledgeSyncError(
                            f"common_knowledge_peer_http_{response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type != "application/json":
                        raise CommonKnowledgeSyncError("common_knowledge_peer_content_type")
                    body = read_bounded_sync_response(
                        response,
                        max_body_bytes=COMMON_KNOWLEDGE_MAX_RESPONSE_BYTES,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        total_timeout_seconds=self.timeout_seconds,
                        deadline=deadline,
                    )
                    active()
                    return body
                finally:
                    timeout_hook.clear(response)

        try:
            body = call_sync_http_with_deadline(
                perform_request,
                timeout=active(),
                on_timeout=timeout_hook.cancel,
            )
            active()
        except CommonKnowledgeSyncError:
            raise
        except HTTPResponseLimitError as error:
            raise CommonKnowledgeSyncError("common_knowledge_peer_response_too_large") from error
        except (TimeoutError, httpx.TimeoutException) as error:
            raise CommonKnowledgeSyncError("common_knowledge_peer_timeout") from error
        except httpx.ConnectError as error:
            raise CommonKnowledgeSyncError("common_knowledge_peer_unavailable") from error
        except httpx.HTTPError as error:
            raise CommonKnowledgeSyncError("common_knowledge_peer_transport") from error
        if body is None:
            return None
        try:
            payload = strict_json_loads(body)
        except (TypeError, ValueError) as error:
            raise CommonKnowledgeSyncError("common_knowledge_peer_invalid_json") from error
        if not isinstance(payload, dict):
            raise CommonKnowledgeSyncError("common_knowledge_peer_invalid_document")
        return payload
