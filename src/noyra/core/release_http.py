"""Public release metadata reads with a total deadline and bounded workers."""

from __future__ import annotations

import time
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit

import httpx

from .deadline import remaining_seconds
from .http import (
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
    validate_response_headers,
)


def read_release_metadata(url: str, *, maximum: int, timeout: float) -> bytes:
    timeout = remaining_seconds(timeout)
    deadline = time.monotonic() + timeout
    hook = SyncHTTPTimeoutHook()

    def active() -> float:
        hook.assert_active()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("release metadata deadline exceeded")
        return remaining

    def trace(event: str, info: dict[str, Any]) -> None:
        if event.endswith(("send_request_headers.started", "send_request_body.started")):
            active()

    def perform() -> bytes:
        active()
        with httpx.Client(
            transport=PublicDNSHTTPTransport(max_connections=1, remaining=active),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            target = url
            for _ in range(6):
                parsed = urlsplit(target)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError("invalid_release_metadata_url")
                with client.stream(
                    "GET",
                    target,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Noyra-release-assurance",
                    },
                    timeout=active(),
                    extensions={"trace": trace},
                ) as response:
                    hook.register(response)
                    try:
                        active()
                        validate_response_headers(response)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("invalid_release_redirect")
                            target = urljoin(target, location)
                            continue
                        if response.status_code >= 400:
                            headers = Message()
                            for key, value in response.headers.items():
                                headers[key] = value
                            raise HTTPError(
                                target,
                                response.status_code,
                                "release metadata error",
                                headers,
                                None,
                            )
                        return read_bounded_sync_response(
                            response,
                            max_body_bytes=maximum,
                            deadline=deadline,
                        )
                    finally:
                        hook.clear(response)
            raise ValueError("release_redirect_limit")

    try:
        return bytes(call_sync_http_with_deadline(perform, timeout=timeout, on_timeout=hook.cancel))
    except httpx.TimeoutException as error:
        raise TimeoutError("release metadata deadline exceeded") from error
    except httpx.HTTPError as error:
        raise URLError("release metadata unavailable") from error
