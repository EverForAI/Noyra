from __future__ import annotations

import asyncio
import ipaddress
import queue
import socket
import ssl
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any

import httpcore
import httpx

DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_HEADER_BYTES = 64_000
DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0

AsyncResolver = Callable[[str, int], Awaitable[Sequence[str]]]
SyncResolver = Callable[[str, int], Sequence[str]]
_SYNC_RESOLVER_GATE = threading.BoundedSemaphore(4)
_SYNC_HTTP_GATE = threading.BoundedSemaphore(4)


class SyncHTTPTimeoutHook:
    """Close a response that outlives a synchronous HTTP deadline.

    The bounded worker used by ``call_sync_http_with_deadline`` cannot be
    forcefully cancelled.  A request-specific hook lets the caller release
    the active response before the timeout is returned, while also handling
    the race where the worker enters the response context after cancellation.
    ``close`` is deliberately invoked outside the lock; response close
    implementations must be short and non-blocking for this contract.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resource: Any | None = None
        self._cancelled = False

    def register(self, resource: Any) -> None:
        close_now = False
        with self._lock:
            if self._cancelled:
                close_now = True
            else:
                self._resource = resource
        if close_now:
            self._close(resource)

    def clear(self, resource: Any | None = None) -> None:
        with self._lock:
            if resource is None or self._resource is resource:
                self._resource = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            resource = self._resource
            self._resource = None
        if resource is not None:
            self._close(resource)

    @staticmethod
    def _close(resource: Any) -> None:
        try:
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        except BaseException:
            # Timeout cleanup must not replace the original timeout/error.
            pass


def _call_sync_with_timeout(function: Callable[..., Any], *args: Any, timeout: float | None) -> Any:
    """Run a potentially blocking resolver without extending an HTTP deadline."""
    if timeout is None:
        return function(*args)
    if timeout <= 0:
        raise TimeoutError("HTTP operation deadline exceeded")
    started = time.monotonic()
    if not _SYNC_RESOLVER_GATE.acquire(timeout=timeout):
        raise TimeoutError("HTTP resolver capacity deadline exceeded")
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        _SYNC_RESOLVER_GATE.release()
        raise TimeoutError("HTTP operation deadline exceeded")
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, function(*args)))
        except BaseException as error:
            result.put((False, error))
        finally:
            _SYNC_RESOLVER_GATE.release()

    worker = threading.Thread(target=invoke, name="noyra-http-resolver", daemon=True)
    try:
        worker.start()
    except BaseException:
        _SYNC_RESOLVER_GATE.release()
        raise
    try:
        succeeded, value = result.get(timeout=remaining)
    except queue.Empty as error:
        raise TimeoutError("HTTP operation deadline exceeded") from error
    if succeeded:
        return value
    raise value


async def _call_sync_resolver_async(
    function: Callable[..., Any],
    *args: Any,
    timeout: float | None,
) -> Any:
    """Run blocking DNS in at most four daemon workers without using asyncio's pool.

    Cancelling ``asyncio.to_thread(getaddrinfo)`` cannot stop the underlying
    resolver and can fill the event loop's shared executor with late DNS work.
    The process-wide resolver gate remains held until each daemon worker really
    exits, so repeated deadlines cannot create an unbounded queue or starve
    unrelated ``to_thread`` operations.
    """
    if timeout is not None and timeout <= 0:
        raise TimeoutError("HTTP operation deadline exceeded")
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while not _SYNC_RESOLVER_GATE.acquire(blocking=False):
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("HTTP resolver capacity deadline exceeded")
        await asyncio.sleep(0.01 if remaining is None else min(0.01, remaining))

    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcome.put((True, function(*args)))
        except BaseException as error:
            outcome.put((False, error))
        finally:
            _SYNC_RESOLVER_GATE.release()

    worker = threading.Thread(target=invoke, name="noyra-http-resolver", daemon=True)
    try:
        worker.start()
    except BaseException:
        _SYNC_RESOLVER_GATE.release()
        raise

    while True:
        try:
            succeeded, value = outcome.get_nowait()
            break
        except queue.Empty:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("HTTP operation deadline exceeded") from None
            await asyncio.sleep(0.01 if remaining is None else min(0.01, remaining))
    if succeeded:
        return value
    raise value


def call_sync_http_with_deadline(
    function: Callable[..., Any],
    *args: Any,
    timeout: float,
    on_timeout: Callable[[], None] | None = None,
) -> Any:
    """Bound a complete synchronous HTTP operation, including headers and body.

    Socket read timeouts are idle limits, not absolute operation deadlines.
    Running the complete request in a process-wide bounded worker gives sync
    callers a real wall-clock deadline while containing any non-cancellable
    late network operation to at most four daemon workers.
    """
    if timeout <= 0:
        raise ValueError("HTTP operation timeout must be positive")
    started = time.monotonic()
    if not _SYNC_HTTP_GATE.acquire(timeout=timeout):
        raise TimeoutError("HTTP operation capacity deadline exceeded")
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        _SYNC_HTTP_GATE.release()
        raise TimeoutError("HTTP operation deadline exceeded")
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcome.put((True, function(*args)))
        except BaseException as error:
            outcome.put((False, error))
        finally:
            _SYNC_HTTP_GATE.release()

    worker = threading.Thread(target=invoke, name="noyra-sync-http", daemon=True)
    try:
        worker.start()
    except BaseException:
        _SYNC_HTTP_GATE.release()
        raise
    try:
        succeeded, value = outcome.get(timeout=remaining)
    except queue.Empty as error:
        if on_timeout is not None:
            with suppress(BaseException):
                on_timeout()
        raise TimeoutError("HTTP operation deadline exceeded") from error
    if succeeded:
        return value
    raise value


class HTTPResponseLimitError(ValueError):
    """The upstream response exceeded an explicit header or body bound."""


def public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host to public addresses without leaving a DNS-to-connect gap."""
    del port
    literal = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(literal)
        if not address.is_global:
            raise OSError("HTTP endpoint resolved to a non-public address")
        return [str(address)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise OSError("HTTP endpoint DNS resolution failed") from error
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0])
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        normalized = str(address)
        if address.is_global and normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise OSError("HTTP endpoint resolved to a non-public address")
    return addresses


def validate_public_addresses(addresses: Sequence[str]) -> list[str]:
    """Validate and normalize a resolver result before it is used to connect.

    The returned list is the exact address set the transport will try.  Keeping
    this operation in the transport (rather than doing a separate preflight in
    a caller) closes the resolver-to-connect rebinding window.
    """
    normalized: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as error:
            raise OSError("HTTP endpoint resolver returned an invalid address") from error
        if not address.is_global:
            raise OSError("HTTP endpoint resolved to a non-public address")
        value = str(address)
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise OSError("HTTP endpoint resolved to a non-public address")
    return normalized


class _PinnedDNSSyncStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, server_hostname: str):
        self._stream = stream
        self._server_hostname = server_hostname

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, timeout=timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout=timeout)

    def close(self) -> None:
        self._stream.close()

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._stream.start_tls(
            ssl_context,
            server_hostname=self._server_hostname,
            timeout=timeout,
        )
        return _PinnedDNSSyncStream(stream, self._server_hostname)


class _PublicDNSSyncBackend(httpcore.NetworkBackend):
    """Resolve once, reject private addresses, and connect to that exact result."""

    def __init__(self, resolver: SyncResolver | None = None) -> None:
        self._backend = httpcore.SyncBackend()
        self._resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        try:
            remaining = None if deadline is None else deadline - time.monotonic()
            raw_addresses = (
                _call_sync_with_timeout(public_addresses, host, port, timeout=remaining)
                if self._resolver is None
                else _call_sync_with_timeout(self._resolver, host, port, timeout=remaining)
            )
            resolved = (
                raw_addresses
                if self._resolver is None
                else validate_public_addresses(raw_addresses)
            )
        except TimeoutError as error:
            raise httpcore.ConnectTimeout("HTTP endpoint DNS resolution timed out") from error
        last_error: BaseException | None = None
        for address in resolved:
            try:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise httpcore.ConnectTimeout("HTTP endpoint connection timed out")
                stream = self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return _PinnedDNSSyncStream(stream, host)
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise OSError(f"public HTTP connection failed for {host}") from last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise OSError("public HTTP transport does not support Unix sockets")


class _PinnedDNSAsyncStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream, server_hostname: str):
        self._stream = stream
        self._server_hostname = server_hostname

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._stream.start_tls(
            ssl_context,
            server_hostname=self._server_hostname,
            timeout=timeout,
        )
        return _PinnedDNSAsyncStream(stream, self._server_hostname)


class _PublicDNSAsyncBackend(httpcore.AsyncNetworkBackend):
    """Async counterpart of the public, pinned DNS backend."""

    def __init__(self, resolver: AsyncResolver | None = None) -> None:
        self._backend = httpcore.AnyIOBackend()
        self._resolver = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if timeout is None:
            if self._resolver is None:
                addresses = await _call_sync_resolver_async(
                    public_addresses,
                    host,
                    port,
                    timeout=None,
                )
            else:
                addresses = validate_public_addresses(await self._resolver(host, port))
            deadline = None
        else:
            deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
            try:
                async with asyncio.timeout(max(0.0, timeout)):
                    if self._resolver is None:
                        addresses = await _call_sync_resolver_async(
                            public_addresses,
                            host,
                            port,
                            timeout=max(0.0, timeout),
                        )
                    else:
                        addresses = validate_public_addresses(await self._resolver(host, port))
            except TimeoutError as error:
                raise httpcore.ConnectTimeout("HTTP endpoint DNS resolution timed out") from error
        last_error: BaseException | None = None
        for address in addresses:
            try:
                remaining = (
                    None if deadline is None else deadline - asyncio.get_running_loop().time()
                )
                if remaining is not None and remaining <= 0:
                    raise httpcore.ConnectTimeout("HTTP endpoint connection timed out")
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return _PinnedDNSAsyncStream(stream, host)
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise OSError(f"public HTTP connection failed for {host}") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise OSError("public HTTP transport does not support Unix sockets")


class PublicDNSHTTPTransport(httpx.HTTPTransport):
    """Synchronous HTTP transport with public DNS pinning per TCP connection."""

    def __init__(
        self,
        *,
        max_connections: int = 16,
        resolver: SyncResolver | None = None,
    ) -> None:
        from httpx._config import create_ssl_context

        ssl_context = create_ssl_context(verify=True, cert=None, trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=min(8, max_connections),
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PublicDNSSyncBackend(resolver),
        )


class PublicDNSAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Asynchronous HTTP transport with public DNS pinning per TCP connection."""

    def __init__(
        self,
        *,
        max_connections: int = 16,
        resolver: AsyncResolver | None = None,
    ) -> None:
        from httpx._config import create_ssl_context

        ssl_context = create_ssl_context(verify=True, cert=None, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=min(8, max_connections),
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PublicDNSAsyncBackend(resolver),
        )


def validate_response_headers(
    response: httpx.Response,
    *,
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
) -> None:
    """Reject an oversized response header block before reading the body."""
    if max_header_bytes <= 0:
        raise ValueError("HTTP response header limit must be positive")
    header_bytes = 2  # Final CRLF after the header fields.
    for name, value in response.headers.multi_items():
        header_bytes += len(name.encode("latin-1")) + len(value.encode("latin-1")) + 4
        if header_bytes > max_header_bytes:
            raise HTTPResponseLimitError("HTTP response headers are too large")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPResponseLimitError("HTTP response content length is invalid") from None
        if declared_size < 0:
            raise HTTPResponseLimitError("HTTP response content length is invalid")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise HTTPResponseLimitError("encoded HTTP responses are unsupported")


async def read_bounded_response(
    response: httpx.Response,
    *,
    max_body_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> bytes:
    """Read a response body without buffering beyond the configured contract.

    The caller should keep the response inside ``AsyncClient.stream`` until this
    function returns.  A timeout is surfaced as ``httpx.ReadTimeout`` so callers
    retain their existing unknown-outcome handling.
    """
    if max_body_bytes <= 0:
        raise ValueError("HTTP response body limit must be positive")
    if total_timeout_seconds <= 0:
        raise ValueError("HTTP response timeout must be positive")
    validate_response_headers(response, max_header_bytes=max_header_bytes)
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPResponseLimitError("HTTP response content length is invalid") from None
        if declared_size > max_body_bytes:
            raise HTTPResponseLimitError("HTTP response body is too large")

    loop = asyncio.get_running_loop()
    end = deadline if deadline is not None else loop.time() + total_timeout_seconds
    remaining = end - loop.time()
    if remaining <= 0:
        raise httpx.ReadTimeout("HTTP response deadline exceeded", request=response.request)
    body = bytearray()
    try:
        async with asyncio.timeout(remaining):
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_body_bytes:
                    raise HTTPResponseLimitError("HTTP response body is too large")
                body.extend(chunk)
    except TimeoutError as error:
        raise httpx.ReadTimeout(
            "HTTP response deadline exceeded", request=response.request
        ) from error
    return bytes(body)


def read_bounded_sync_response(
    response: httpx.Response,
    *,
    max_body_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> bytes:
    """Synchronous bounded response reader with a hard elapsed-time deadline."""
    if max_body_bytes <= 0:
        raise ValueError("HTTP response body limit must be positive")
    if total_timeout_seconds <= 0:
        raise ValueError("HTTP response timeout must be positive")
    validate_response_headers(response, max_header_bytes=max_header_bytes)
    declared = response.headers.get("content-length")
    if declared is not None and int(declared) > max_body_bytes:
        raise HTTPResponseLimitError("HTTP response body is too large")

    end = deadline if deadline is not None else time.monotonic() + total_timeout_seconds
    if end - time.monotonic() <= 0:
        raise httpx.ReadTimeout("HTTP response deadline exceeded", request=response.request)
    body = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() >= end:
            raise httpx.ReadTimeout("HTTP response deadline exceeded", request=response.request)
        if len(body) + len(chunk) > max_body_bytes:
            raise HTTPResponseLimitError("HTTP response body is too large")
        body.extend(chunk)
    if time.monotonic() > end:
        raise httpx.ReadTimeout("HTTP response deadline exceeded", request=response.request)
    return bytes(body)
