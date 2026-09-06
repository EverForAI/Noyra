from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlsplit, urlunsplit

import httpx

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    HTTPResponseLimitError,
    PublicDNSAsyncHTTPTransport,
    read_bounded_response,
    validate_public_addresses,
    validate_response_headers,
)
from noyra.core.types import content_hash, new_id, utc_now

from .errors import FetchError, UnsafeSourceError, WorldStateConflictError
from .types import FetchedDocument, SourceRecord, SourceStatus, SourceType

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
ALLOWED_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
        "text/plain",
    }
)
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I)),
    ("system_impersonation", re.compile(r"\b(system|developer)\s+(message|prompt)\b", re.I)),
    ("tool_coercion", re.compile(r"\b(call|invoke|use)\s+(the\s+)?(tool|function)\b", re.I)),
    ("secret_extraction", re.compile(r"\b(api\s*key|password|private\s+key|secret)\b", re.I)),
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
        if normalized == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1
        if normalized == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        text = " ".join(data.split())
        self.text_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)


def canonical_public_url(url: str) -> str:
    if not url.strip() or len(url) > 2_048:
        raise UnsafeSourceError("source URL is missing or too long")
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise UnsafeSourceError("world sources must use an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise UnsafeSourceError("source URL cannot contain credentials or a fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise UnsafeSourceError("source port is invalid") from error
    if port not in {None, 443}:
        raise UnsafeSourceError("world sources must use the standard HTTPS port")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise UnsafeSourceError("source hostname is invalid") from error
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise UnsafeSourceError("source address is not globally routable")
    sensitive_query = re.compile(r"(token|key|secret|password|signature|auth)", re.I)
    if any(
        sensitive_query.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise UnsafeSourceError("source URL query cannot contain credential-like parameters")
    netloc = f"[{host}]" if ":" in host else host
    path = parsed.path or "/"
    return urlunsplit(SplitResult("https", netloc, path, parsed.query, ""))


class SourceRegistry:
    def __init__(self, database: Database):
        self.database = database

    def register(
        self,
        subject_id: str,
        name: str,
        url: str,
        source_type: SourceType,
        *,
        trust_score: float = 0.5,
        status: SourceStatus = "candidate",
        reason: str = "source discovered",
    ) -> SourceRecord:
        normalized_url = canonical_public_url(url)
        self._validate(name, trust_score, status, reason)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM world_sources WHERE subject_id = ? AND url = ?",
                (subject_id, normalized_url),
            ).fetchone()
            if existing is not None:
                if existing["name"] != name or existing["source_type"] != source_type:
                    raise WorldStateConflictError(
                        "source URL already has different identity metadata"
                    )
                return self._from_row(existing)
            source_id = new_id("src")
            now = utc_now()
            state_hash = self._record_hash(name, normalized_url, source_type, trust_score, status)
            connection.execute(
                """INSERT INTO world_sources(
                    source_id, subject_id, name, url, source_type, trust_score,
                    status, state_hash, current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    source_id,
                    subject_id,
                    name,
                    normalized_url,
                    source_type,
                    trust_score,
                    status,
                    state_hash,
                    now,
                    now,
                ),
            )
            self._insert_revision(connection, source_id, 1, trust_score, status, reason, now)
            return self._load_connection(connection, source_id, subject_id=subject_id)

    def revise(
        self,
        source_id: str,
        *,
        subject_id: str,
        trust_score: float,
        status: SourceStatus,
        reason: str,
        expected_revision: int | None = None,
    ) -> SourceRecord:
        self._validate("existing", trust_score, status, reason)
        with self.database.transaction() as connection:
            row = self._get_row(connection, source_id, subject_id=subject_id)
            revision = int(row["current_revision"])
            if expected_revision is not None and revision != expected_revision:
                raise WorldStateConflictError("source revision changed before update")
            revision += 1
            now = utc_now()
            state_hash = self._record_hash(
                row["name"], row["url"], row["source_type"], trust_score, status
            )
            self._insert_revision(connection, source_id, revision, trust_score, status, reason, now)
            connection.execute(
                """UPDATE world_sources SET trust_score = ?, status = ?, state_hash = ?,
                    current_revision = ?, updated_at = ? WHERE source_id = ? AND subject_id = ?""",
                (trust_score, status, state_hash, revision, now, source_id, subject_id),
            )
            return self._load_connection(connection, source_id, subject_id=subject_id)

    def get(self, source_id: str, *, subject_id: str) -> SourceRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, source_id, subject_id=subject_id)

    def active(self, subject_id: str) -> list[SourceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM world_sources WHERE subject_id = ? AND status = 'active' "
                "ORDER BY trust_score DESC, name",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _validate(name: str, trust: float, status: str, reason: str) -> None:
        if not name.strip() or not reason.strip() or len(name) > 256 or len(reason) > 10_000:
            raise ValueError("source name or reason is invalid")
        if not 0 <= trust <= 1:
            raise ValueError("source trust must be between zero and one")
        if status not in {"candidate", "active", "blocked"}:
            raise ValueError(f"invalid source status: {status}")

    @staticmethod
    def _revision_hash(trust: float, status: str) -> str:
        return content_hash({"trust_score": float(trust), "status": status})

    @staticmethod
    def _record_hash(name: str, url: str, source_type: str, trust: float, status: str) -> str:
        return content_hash(
            {
                "name": name,
                "url": url,
                "source_type": source_type,
                "trust_score": float(trust),
                "status": status,
            }
        )

    @classmethod
    def _insert_revision(
        cls,
        connection: Any,
        source_id: str,
        revision: int,
        trust: float,
        status: str,
        reason: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO world_source_revisions(
                revision_id, source_id, revision_number, trust_score,
                status, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("srev"),
                source_id,
                revision,
                trust,
                status,
                reason,
                cls._revision_hash(trust, status),
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, source_id: str, *, subject_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM world_sources WHERE source_id = ? AND subject_id = ?",
            (source_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"world source not found: {source_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, source_id: str, *, subject_id: str) -> SourceRecord:
        return cls._from_row(cls._get_row(connection, source_id, subject_id=subject_id))

    @classmethod
    def _from_row(cls, row: Any) -> SourceRecord:
        try:
            trust_score = float(row["trust_score"])
            current_revision = int(row["current_revision"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"world source durable state is invalid: {row['source_id']}"
            ) from error
        if not math.isfinite(trust_score):
            raise IntegrityError(f"world source trust score is invalid: {row['source_id']}")
        expected = cls._record_hash(
            row["name"],
            row["url"],
            row["source_type"],
            trust_score,
            row["status"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"world source state hash mismatch: {row['source_id']}")
        return SourceRecord(
            row["source_id"],
            row["subject_id"],
            row["name"],
            row["url"],
            row["source_type"],
            trust_score,
            row["status"],
            row["state_hash"],
            current_revision,
            row["created_at"],
            row["updated_at"],
        )


class SafeWebReader:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        verify_peer_address: bool = True,
        max_response_bytes: int = 2_000_000,
        max_text_chars: int = 500_000,
        total_timeout_seconds: float = 30.0,
    ):
        if not 1_024 <= max_response_bytes <= 20_000_000:
            raise ValueError("web response limit must be 1KB-20MB")
        if not 1_024 <= max_text_chars <= 2_000_000:
            raise ValueError("web text limit must be 1KB-2M characters")
        self._resolver = resolver or self._default_resolver
        if not 0 < total_timeout_seconds <= 300:
            raise ValueError("web fetch timeout is invalid")
        self.total_timeout_seconds = total_timeout_seconds
        self._pinned_client = client is None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(total_timeout_seconds, connect=min(10, total_timeout_seconds)),
            limits=httpx.Limits(max_connections=8),
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSAsyncHTTPTransport(
                max_connections=8,
                resolver=self._resolver,
            ),
        )
        self._verify_peer_address = verify_peer_address
        self.max_response_bytes = max_response_bytes
        self.max_text_chars = max_text_chars

    async def fetch(self, source: SourceRecord) -> FetchedDocument:
        if source.status != "active":
            raise UnsafeSourceError("only active world sources can be fetched")
        url = canonical_public_url(source.url)
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.total_timeout_seconds
        # Keep an early, caller-visible validation error while the default
        # transport repeats the same resolver inside connect_tcp and pins that
        # exact result.  The preflight is therefore only a usability check; it
        # is not the security boundary and cannot reintroduce DNS TOCTOU.
        if not self._pinned_client:
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    validate_public_addresses(await self._resolver(parsed.hostname, 443))
            except TimeoutError:
                raise FetchError("world_fetch_timeout") from None
            except OSError as error:
                raise UnsafeSourceError(str(error)) from error

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                async with self._client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "text/html,application/rss+xml,application/json,text/plain;q=0.8",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Noyra-Research/0.1.0",
                    },
                    follow_redirects=False,
                ) as response:
                    try:
                        validate_response_headers(
                            response,
                            max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        )
                    except HTTPResponseLimitError as error:
                        raise FetchError("world_fetch_response_too_large") from error
                    if response.status_code != 200:
                        raise FetchError(f"world_fetch_http_{response.status_code}")
                    peer = self._peer_address(response)
                    if self._verify_peer_address and peer is None:
                        raise UnsafeSourceError("world fetch peer address could not be verified")
                    if peer is not None:
                        try:
                            peer_address = ipaddress.ip_address(peer)
                        except ValueError as error:
                            raise UnsafeSourceError(
                                "world fetch peer address is invalid"
                            ) from error
                        if not peer_address.is_global:
                            raise UnsafeSourceError("world fetch connected to a non-public address")
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if media_type not in ALLOWED_MEDIA_TYPES:
                        raise FetchError("world_fetch_media_type_rejected")
                    try:
                        body = await read_bounded_response(
                            response,
                            max_body_bytes=self.max_response_bytes,
                            max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                            total_timeout_seconds=self.total_timeout_seconds,
                            deadline=deadline,
                        )
                    except HTTPResponseLimitError as error:
                        raise FetchError("world_fetch_response_too_large") from error
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                title, content = self._extract(media_type, text)
                content = content[: self.max_text_chars]
                signals = tuple(
                    name for name, pattern in INJECTION_PATTERNS if pattern.search(content)
                )
                return FetchedDocument(
                    url=url,
                    title=title,
                    content=content,
                    content_hash=content_hash(content),
                    media_type=media_type,
                    injection_signals=signals,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    fetched_at=utc_now(),
                )
        except (FetchError, UnsafeSourceError):
            raise
        except TimeoutError:
            raise FetchError("world_fetch_timeout") from None
        except httpx.ReadTimeout:
            raise FetchError("world_fetch_timeout") from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise FetchError("world_fetch_connect_failed") from None
        except httpx.HTTPError:
            raise FetchError("world_fetch_transport_failed") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _peer_address(response: httpx.Response) -> str | None:
        stream = response.extensions.get("network_stream")
        if stream is None or not hasattr(stream, "get_extra_info"):
            return None
        peer = stream.get_extra_info("server_addr")
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        if isinstance(peer, str):
            return peer
        return None

    @staticmethod
    async def _default_resolver(host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(record[4][0] for record in records))

    @staticmethod
    def _extract(media_type: str, text: str) -> tuple[str | None, str]:
        if media_type == "application/json":
            try:
                value = json.loads(text)
            except (ValueError, RecursionError) as error:
                raise FetchError("world_fetch_json_invalid") from error
            try:
                return None, json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError) as error:
                raise FetchError("world_fetch_json_invalid") from error
        if media_type == "text/plain":
            return None, "\n".join(line.strip() for line in text.splitlines() if line.strip())
        extractor = _TextExtractor()
        try:
            extractor.feed(text)
        except ValueError as error:
            raise FetchError("world_fetch_markup_invalid") from error
        title = " ".join(extractor.title_parts).strip() or None
        content = "\n".join(extractor.text_parts)
        return title, content
