from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import httpx
import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel, release_assurance, release_http
from noyra.core import http as bounded_http
from noyra.core.deadline import operation_deadline, remaining_seconds
from noyra.core.http import HTTPResponseLimitError
from noyra.core.upgrade import UpgradeError, UpgradeManager
from noyra.service import NoyraHTTPServer, ServiceSettings


def install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.BaseTransport) -> None:
    monkeypatch.setattr(release_http, "PublicDNSHTTPTransport", lambda **kwargs: transport)


def test_release_drip_deadline_closes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = threading.Event()

    class Drip(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(50):
                if closed.wait(0.025):
                    break
                yield b" "

        def close(self) -> None:
            closed.set()

    install_transport(
        monkeypatch, httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip()))
    )
    started = time.monotonic()
    with operation_deadline(0.12), pytest.raises(TimeoutError):
        release_assurance.github_json("https://api.github.com/repos/example/noyra/commits/main")
    assert time.monotonic() - started < 0.8
    assert closed.wait(1)


def test_actual_httpcore_header_drip_releases_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stopped = threading.Event()
    finished = threading.Event()

    def serve() -> None:
        try:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                peer.recv(8192)
                for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 100:
                    if stopped.wait(0.02):
                        break
                    peer.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    # Loopback fixture only: use the actual httpcore header parser and socket
    # reads, replacing public DNS and the local TLS handshake.
    monkeypatch.setattr(bounded_http, "public_addresses", lambda *a, **k: ["127.0.0.1"])
    monkeypatch.setattr(bounded_http._PinnedDNSSyncStream, "start_tls", lambda self, *a, **k: self)
    try:
        with pytest.raises(TimeoutError):
            release_http.read_release_metadata(
                f"https://example.test:{listener.getsockname()[1]}/", maximum=100, timeout=0.15
            )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not any(t.name == "noyra-sync-http" for t in threading.enumerate()):
                break
            time.sleep(0.01)
        assert not any(t.name == "noyra-sync-http" for t in threading.enumerate())
    finally:
        stopped.set()
        listener.close()
        worker.join(2)
    assert finished.is_set()


def test_stable_sequence_shares_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[float] = []

    def metadata(url: str) -> Any:
        budgets.append(remaining_seconds(20))
        time.sleep(0.03)
        if url.endswith("latest"):
            return {"tag_name": "v1", "published_at": "2026-10-11T00:00:00Z"}
        return {"sha": "a" * 40, "commit": {"committer": {"date": "2026-10-11T00:00:00Z"}}}

    monkeypatch.setattr(release_assurance, "github_json", metadata)
    with operation_deadline(1):
        release_assurance.stable_metadata("example", "noyra")
    assert len(budgets) == 2 and budgets[1] < budgets[0] - 0.02


def test_late_connection_cannot_send_headers_after_metadata_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    sent: list[int] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                release.wait(2)
                request.extensions["trace"]("http11.send_request_headers.started", {})
                sent.append(1)
                return httpx.Response(200, json={})
            finally:
                finished.set()

    install_transport(monkeypatch, Transport())
    try:
        with pytest.raises(TimeoutError):
            release_http.read_release_metadata("https://example.com", maximum=100, timeout=0.1)
        assert entered.is_set()
    finally:
        release.set()
    assert finished.wait(2)
    assert not sent


@pytest.mark.parametrize("kind", ["oversize", "429", "redirect", "stable404", "rate403"])
def test_metadata_limits_and_upgrade_error_contracts(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if kind == "stable404":
            return httpx.Response(404)
        if kind == "rate403":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
        if kind == "429":
            return httpx.Response(429)
        if kind == "oversize":
            return httpx.Response(200, content=b"x" * 100)
        return httpx.Response(302, headers={"location": "http://localhost/forbidden"})

    install_transport(monkeypatch, httpx.MockTransport(respond))
    if kind in {"429", "stable404", "rate403"}:
        manager = UpgradeManager(
            github_owner="example",
            github_repo="noyra",
            channel="stable" if kind == "stable404" else "development-main",
        )
        expected = (
            "upgrade_no_stable_release" if kind == "stable404" else "upgrade_remote_rate_limited"
        )
        with pytest.raises(UpgradeError, match=expected):
            manager._fetch_github_metadata()
    else:
        with pytest.raises(HTTPResponseLimitError if kind == "oversize" else ValueError):
            release_http.read_release_metadata("https://example.com", maximum=10, timeout=1)
    assert len(calls) == 1


@pytest.fixture
def server(tmp_path: Path) -> Iterator[NoyraHTTPServer]:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="request-deadline",
        genesis_hash="a" * 64,
        host="127.0.0.1",
        port=0,
        request_timeout_seconds=1,
        admin_token=SecretStr("synthetic-test-token-with-sufficient-entropy"),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    service = NoyraHTTPServer(kernel, settings)
    service.start()
    try:
        yield service
    finally:
        service.close()
        kernel.close()


@pytest.mark.parametrize("phase", ["request_line", "headers", "body", "webhook"])
def test_real_inbound_drip_releases_worker_at_deadline(
    server: NoyraHTTPServer,
    phase: str,
) -> None:
    peer = socket.create_connection(server.address, timeout=3)
    if phase == "request_line":
        prefix, drip = b"", b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n"
    elif phase == "headers":
        prefix, drip = b"GET /health/live HTTP/1.1\r\n", b"X-Slow: " + b"a" * 100
    else:
        path = "/admin/session" if phase == "body" else "/webhooks/qq/synthetic"
        prefix = f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n".encode()
        drip = b" " * 100
    started = time.monotonic()
    try:
        peer.sendall(prefix)
        for byte in drip:
            try:
                peer.sendall(bytes([byte]))
            except OSError:
                break
            if time.monotonic() - started > 1.3:
                break
            time.sleep(0.035)
        assert server.server.wait_handlers(timeout=0.5)
        assert time.monotonic() - started < 2
    finally:
        peer.close()
    with urlopen(
        f"http://{server.address[0]}:{server.address[1]}/health/live", timeout=3
    ) as response:
        assert response.status == 200


def test_fast_login_retains_auth_rejection_and_bridge_uses_remaining_budget(
    server: NoyraHTTPServer,
) -> None:
    from urllib.request import Request

    request = Request(
        f"http://{server.address[0]}:{server.address[1]}/admin/session",
        data=b'{"token":"invalid"}',
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=3)
    assert error.value.code == 401
    started = time.monotonic()
    with operation_deadline(0.1), pytest.raises(TimeoutError):
        server.run_async_from_handler(asyncio.sleep(1))
    assert time.monotonic() - started < 0.5
