from __future__ import annotations

import io
import json
import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.runtime_logging import install_log_safety
from noyra.core.types import content_hash
from noyra.http_access_logging import http_log_fields
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.service_contract import route_contracts

TOKEN = "synthetic-access-log-token-with-sufficient-entropy"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[NoyraHTTPServer]:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="access-log-test",
        genesis_hash=content_hash("access-log-test"),
        port=0,
        operator_token=SecretStr(TOKEN),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    http = NoyraHTTPServer(kernel, settings)
    http.start()
    try:
        yield http
    finally:
        http.close()
        kernel.close()


def call(
    server: NoyraHTTPServer,
    path: str,
    *,
    authorized: bool = True,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"http://127.0.0.1:{server.address[1]}{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}" if authorized else "Bearer synthetic-invalid",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


@pytest.mark.parametrize("version", ["", "/v1"])
def test_private_search_keeps_results_and_auth_but_never_logs_content(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The source contract must be safe even without downstream log redaction.
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    caplog.set_level(logging.INFO, logger="noyra.service")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("noyra.service")
    logger.addHandler(handler)
    try:
        content = "synthetic-private-phrase 私密名字与聊天原文"
        status, window = call(
            server, f"/api{version}/admin/conversations", payload={"title": content}
        )
        assert status == 200
        identifier = window["conversation_id"]
        assert (
            call(
                server,
                f"/api{version}/interactions",
                payload={"content": content, "conversation_id": identifier},
            )[0]
            == 201
        )
        query = urlencode({"query": content, "opaque": "synthetic-private-opaque"})
        path = f"/api{version}/admin/conversations/{identifier}/messages?{query}"
        assert call(server, path, authorized=False)[0] == 401
        status, results = call(server, path)
        assert status == 200
        assert [row["content"] for row in results["items"]] == [content]
        missing = f"/api{version}/admin/conversations/synthetic-private-id/messages?{query}"
        assert call(server, missing)[0] == 404
        records = [record for record in caplog.records if record.name == "noyra.service"]
        for output in (stream.getvalue(), caplog.text, repr([r.__dict__ for r in records])):
            for secret in (TOKEN, content, quote(content), identifier, "synthetic-private"):
                assert secret not in output
            assert "route=/api/v1/admin/conversations/{conversationId}/messages" in output
            for code in (200, 401, 404):
                assert f"status={code}" in output
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize(
    ("target", "status", "route"),
    [
        ("/synthetic-private-unknown?query=synthetic-private", 404, "unmatched"),
        (
            "/webhooks/qq/synthetic-private-token?secret=synthetic-private",
            400,
            "/api/v1/webhooks/{channel}/{transportId}",
        ),
        (
            "/api/v1/webhooks/qq/synthetic-private-token?secret=synthetic-private",
            400,
            "/api/v1/webhooks/{channel}/{transportId}",
        ),
    ],
)
def test_unknown_and_webhook_targets_never_log_paths_or_parameters(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    target: str,
    status: int,
    route: str,
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    assert call(server, target, payload={})[0] == status
    assert "synthetic-private" not in repr([r.__dict__ for r in caplog.records])
    assert f"route={route}" in caplog.text
    assert f"status={status}" in caplog.text


def raw_call(server: NoyraHTTPServer, line: bytes) -> bytes:
    with socket.create_connection(server.address, timeout=5) as connection:
        connection.sendall(line + b"\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        response = bytearray()
        while True:
            try:
                data = connection.recv(8192)
            except ConnectionResetError:
                # A parser limit can close before consuming the remaining
                # request. The response and log status must still be checked.
                break
            if not data:
                break
            response.extend(data)
    return bytes(response)


@pytest.mark.parametrize(
    ("line", "status", "event"),
    [
        (b"GET /health/live?query=synthetic-private\x1b[2J\x7f HTTP/1.1", 200, "request"),
        (b"GET /synthetic-private\x1b[2J HTTP/1.1", 404, "request"),
        (b"synthetic-private\x1b[2J /health/live HTTP/1.1", 501, "response_error"),
        (b"GET /synthetic-private HTTP/synthetic-private\x1b[2J", 400, "response_error"),
        (b"GET /synthetic-private extra HTTP/1.1", 400, "response_error"),
        (b"GET /synthetic-private HTTP/2.0", 505, "response_error"),
        (b"GET /" + b"synthetic-private" * 4_500 + b" HTTP/1.1", 414, "response_error"),
        # Cross our 64k combined-head policy before the stdlib 65536 line cap.
        (b"GET /" + b"x" * 64_000 + b" HTTP/1.1", 414, "response_error"),
        (
            b"GET /health/live HTTP/1.1\r\n"
            + (b"X-Private: synthetic-private" + b"x" * 1_000 + b"\r\n") * 65,
            431,
            "response_error",
        ),
        (b"HEAD /health/live HTTP/1.1\r\nX-Private: " + b"x" * 70_000, 431, "response_error"),
        (b"GET /health/live HTTP/1.1\r\nX-Private: " + b"x" * 70_000, 431, "response_error"),
    ],
    ids=[
        "query-controls",
        "path-controls",
        "unknown-method",
        "invalid-version",
        "malformed-line",
        "unsupported-version",
        "long-target",
        "target-over-policy-before-stdlib",
        "aggregate-short-headers",
        "head-long-header",
        "long-header",
    ],
)
def test_raw_protocol_and_control_data_never_reach_log_handlers(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    line: bytes,
    status: int,
    event: str,
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    response = raw_call(server, line)
    if response.startswith(b"HTTP/"):
        assert f" {status} ".encode() in response.split(b"\n", 1)[0]
    else:
        # stdlib retains HTTP/0.9 while rejecting a malformed/unsupported
        # version, and returns an HTML error body without a status line.
        assert f"Error code: {status}".encode() in response
    records = [record for record in caplog.records if record.name == "noyra.service"]
    assert records
    for record in records:
        output = repr(record.__dict__)
        assert "synthetic-private" not in output
        assert "\\x1b" not in output
        assert "\\x7f" not in output
        assert not any(ord(c) < 32 or ord(c) == 127 for c in record.getMessage())
    assert f"event={event}" in caplog.text
    assert f"status={status}" in caplog.text


def test_head_budget_rejection_never_dispatches_and_server_recovers(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from noyra.http_routes import get

    # Force a rejection in the very first read, before stdlib has even set
    # requestline/command/version. A coalesced valid request line does not make
    # a header overflow a 414. Neither the rejected request nor a pipelined
    # suffix may reach routing, and the next independent connection must work.
    with monkeypatch.context() as patch:
        patch.setattr("noyra.http_transport.MAX_REQUEST_HEADER_BYTES", 128)
        dispatch = Mock(wraps=get.dispatch)
        patch.setattr(get, "dispatch", dispatch)
        caplog.set_level(logging.INFO, logger="noyra.service")
        response = raw_call(
            server,
            b"GET /health/live HTTP/1.1\r\nX-Private: "
            + b"synthetic-private" * 50
            + b"\r\n\r\nGET /health/live HTTP/1.1",
        )
        assert response.startswith(b"HTTP/1.0 431 ")
        assert b"Connection: close" in response
        assert response.count(b"HTTP/1.0 ") == 1
        dispatch.assert_not_called()
        assert "synthetic-private" not in caplog.text
        assert "event=response_error" in caplog.text
        assert "route=unmatched status=431" in caplog.text
        # Restore the normal cap so urllib's ordinary headers fit.
    # The next ordinary connection uses the normal cap and dispatch path.
    assert call(server, "/health/live", authorized=False)[0] == 200


def test_every_api_log_template_hides_parameters_and_preserves_version_aliases() -> None:
    for contract in route_contracts():
        path = contract.path
        while "{" in path:
            left, _, remaining = path.partition("{")
            _, _, right = remaining.partition("}")
            path = left + "synthetic-private-id" + right
        aliases = {path, path.replace("/api/v1/", "/api/", 1)}
        if "/webhooks/" in path:
            aliases.add(path.replace("/api/v1/webhooks/", "/webhooks/", 1))
        for alias in aliases:
            fields = http_log_fields(contract.method, alias + "?query=synthetic-private", 200)
            assert fields[1] == contract.path
            assert "synthetic-private" not in repr(fields)
            assert fields[0] == contract.method
            assert fields[2] == 200


@pytest.mark.parametrize(
    ("method", "target", "code", "expected"),
    [
        ("GET", "/health/live?query=\x1b[2J", "200", ("GET", "/health/live", 200)),
        ("POST", "/admin/session", 401, ("POST", "/admin/session", 401)),
        ("synthetic-private\x1b[2J", "/health/live", "400", ("UNKNOWN", "/health/live", 400)),
        (None, None, True, ("UNKNOWN", "unmatched", "unavailable")),
        ([], {}, "200\nprivate", ("UNKNOWN", "unmatched", "unavailable")),
        ("GET", "https://synthetic-private/health/live", 600, ("GET", "unmatched", "unavailable")),
        ("GET", "/health/live/" + "x" * 9_000, 404, ("GET", "unmatched", 404)),
        ("GET", "/synthetic-private\x1b[2J", 404, ("GET", "unmatched", 404)),
    ],
)
def test_log_fields_accept_only_safe_constants_and_numeric_status(
    method: object, target: object, code: object, expected: tuple[str, str, int | str]
) -> None:
    assert http_log_fields(method, target, code) == expected


def test_handler_diagnostic_hooks_discard_exception_and_format_arguments(
    server: NoyraHTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    install_log_safety()
    caplog.set_level(logging.INFO, logger="noyra.service")
    handler = object.__new__(server._handler_type())
    handler.command = "GET"
    handler.path = "/health/live?query=synthetic-private"
    handler.log_message("synthetic-private %s", "synthetic-private\x1b[2J")
    handler.log_error("unknown synthetic-private %s", ValueError("synthetic-private\x1b[2J"))
    handler.log_error("Request timed out: %r", TimeoutError("synthetic-private\x1b[2J"))
    for output in (caplog.text, repr([r.__dict__ for r in caplog.records])):
        assert "synthetic-private" not in output
        assert "event=diagnostic" in output
        assert "event=protocol_error" in output
        assert "event=timeout" in output
        assert "route=unmatched" in output


def test_malformed_keepalive_request_cannot_reuse_previous_log_route(
    server: NoyraHTTPServer, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    # Exercise a reused handler, although production currently speaks HTTP/1.0.
    handler_type = type(
        "KeepAliveHandler", (server._handler_type(),), {"protocol_version": "HTTP/1.1"}
    )
    monkeypatch.setattr(server.server, "RequestHandlerClass", handler_type)
    with socket.create_connection(server.address, timeout=5) as connection:
        connection.sendall(
            b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /synthetic-private HTTP/synthetic-private\x1b[2J\r\n"
            b"Host: localhost\r\nConnection: close\r\n\r\n"
        )
        response = bytearray()
        while chunk := connection.recv(8192):
            response.extend(chunk)
    assert b"HTTP/1.1 200 OK" in response
    assert b"Error code: 400" in response
    assert "event=request method=GET route=/health/live status=200" in caplog.text
    assert "event=response_error method=UNKNOWN route=unmatched status=400" in caplog.text
    assert "synthetic-private" not in repr([r.__dict__ for r in caplog.records])
