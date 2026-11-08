from __future__ import annotations

import json
import logging
import re
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core.errors import IntegrityError
from noyra.service import NoyraHTTPServer
from test_http_access_logging import server as server


def raw(server: NoyraHTTPServer, request: bytes) -> bytes:
    with socket.create_connection(server.address, timeout=5) as connection:
        connection.sendall(request)
        result = bytearray()
        while chunk := connection.recv(8192):
            result.extend(chunk)
    return bytes(result)


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (RuntimeError("synthetic-private"), 500),
        (sqlite3.OperationalError("synthetic-private"), 503),
        (IntegrityError("synthetic-private"), 503),
    ],
)
def test_unexpected_pre_response_failure_has_fixed_status_id_and_safe_diagnostics(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    status: int,
) -> None:
    # Safety must hold before/without the global logging redactor.
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    caplog.set_level(logging.INFO, logger="noyra.service")
    handler = server.server.RequestHandlerClass

    def broken(self: Any) -> None:
        raise failure

    with patch.object(handler, "do_GET", new=broken):
        response = raw(
            server,
            b"GET /api/state?secret=synthetic-private HTTP/1.1\r\nHost: localhost\r\n"
            b"X-Request-ID: synthetic-private\r\nAuthorization: Bearer synthetic-private\r\n\r\n",
        )
    assert response.startswith(f"HTTP/1.0 {status} ".encode())
    head, body = response.split(b"\r\n\r\n", 1)
    payload = json.loads(body)
    assert payload["error"] == ("service_unavailable" if status == 503 else "internal_error")
    assert re.fullmatch(r"[0-9a-f]{32}", payload["request_id"])
    assert b"X-Request-ID: " + payload["request_id"].encode() in head
    assert b"Cache-Control: no-store" in head and b"Connection: close" in head
    assert "synthetic-private" not in repr([record.__dict__ for record in caplog.records])
    assert payload["request_id"] in caplog.text and "broken" in caplog.text
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)


def test_failure_after_headers_never_appends_second_http_response_and_logs_safe_frames(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    handler = server.server.RequestHandlerClass

    def broken(self: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"partial")
        raise RuntimeError("synthetic-private after headers")

    with patch.object(handler, "do_GET", new=broken):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.count(b"HTTP/1.0 ") == 1 and response.endswith(b"partial")
    assert "after_headers" in caplog.text and "broken" in caplog.text
    assert "synthetic-private" not in capsys.readouterr().err


def test_setup_failure_uses_sanitized_server_fallback_without_stderr_traceback(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    with (
        patch.object(
            server.server.RequestHandlerClass,
            "setup",
            side_effect=RuntimeError("synthetic-private setup"),
        ),
        socket.create_connection(server.address, timeout=5) as connection,
    ):
        connection.recv(8192)
    assert "synthetic-private" not in capsys.readouterr().err
    assert "server_failure" in caplog.text and "RuntimeError" in caplog.text


def test_buffered_headers_are_discarded_before_error_response(server: NoyraHTTPServer) -> None:
    def broken(self: Any) -> None:
        self.send_response(200)
        self.send_header("Set-Cookie", "synthetic-private=stale")
        self.send_header("Content-Length", "999")
        raise ValueError("synthetic-private after buffered headers")

    with patch.object(server.server.RequestHandlerClass, "do_GET", new=broken):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.0 500 ") and response.count(b"HTTP/1.0 ") == 1
    assert b"Set-Cookie" not in response and b"synthetic-private" not in response
    head, body = response.split(b"\r\n\r\n", 1)
    assert f"Content-Length: {len(body)}".encode() in head
    assert json.loads(body)["error"] == "internal_error"


def test_serialization_failure_does_not_reenter_broken_json_helper(server: NoyraHTTPServer) -> None:
    def broken(self: Any) -> None:
        self._json(200, {"not_json": object()})

    with patch.object(server.server.RequestHandlerClass, "do_GET", new=broken):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.0 500 ")
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["error"] == "internal_error"


def test_partial_header_write_never_gets_replaced(
    server: NoyraHTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")

    def broken(self: Any) -> None:
        self.send_response(200)
        original = self.wfile.write

        def partial(buffer: bytes) -> Any:
            original(buffer[:12])
            raise ValueError("synthetic-private partial socket writer")

        with patch.object(self.wfile, "write", side_effect=partial):
            self.end_headers()

    with patch.object(server.server.RequestHandlerClass, "do_GET", new=broken):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response == b"HTTP/1.0 200"
    assert b"500" not in response and "after_headers" in caplog.text


def test_unread_post_and_pipeline_close_without_retrying_mutation(server: NoyraHTTPServer) -> None:
    calls = []

    def broken(self: Any) -> None:
        calls.append(True)
        raise RuntimeError("synthetic-private before body consumption")

    with patch.object(server.server.RequestHandlerClass, "do_POST", new=broken):
        response = raw(
            server,
            b"POST /api/participants/login HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 4\r\n\r\nbody"
            b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
    assert calls == [True] and response.count(b"HTTP/1.0 ") == 1
    assert response.startswith(b"HTTP/1.0 500 ") and b"Connection: close" in response
    healthy = raw(server, b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert healthy.startswith(b"HTTP/1.0 200 ")


def test_head_error_has_no_body(server: NoyraHTTPServer) -> None:
    def broken(self: Any) -> None:
        raise KeyError("synthetic-private HEAD")

    with patch.object(server.server.RequestHandlerClass, "do_HEAD", new=broken, create=True):
        response = raw(server, b"HEAD /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    head, body = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.0 500 ") and body == b""
    assert b"X-Request-ID:" in head


def test_keepalive_requests_have_distinct_server_ids_without_stale_response_state(
    server: NoyraHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cast(type[BaseHTTPRequestHandler], server.server.RequestHandlerClass)
    handler = type("KeepAlive", (original,), {"protocol_version": "HTTP/1.1"})
    count = []

    def response(self: Any) -> None:
        count.append(True)
        if len(count) == 1:
            self._json(200, {"ok": True})
        else:
            raise RuntimeError("synthetic-private second request")

    monkeypatch.setattr(server.server, "RequestHandlerClass", handler)
    with patch.object(handler, "do_GET", new=response):
        data = raw(
            server,
            b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
    assert data.count(b"HTTP/1.1 ") == 2
    ids = re.findall(rb"X-Request-ID: ([0-9a-f]{32})", data)
    assert len(ids) == 2 and ids[0] != ids[1]
    assert b"HTTP/1.1 500 " in data and b"synthetic-private" not in data


def test_error_response_failure_closes_once_without_recursing(
    server: NoyraHTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")

    def broken(self: Any) -> None:
        raise ValueError("synthetic-private original failure")

    with (
        patch.object(server.server.RequestHandlerClass, "do_GET", new=broken),
        patch.object(
            server.server.RequestHandlerClass,
            "end_headers",
            side_effect=RuntimeError("synthetic-private fallback failure"),
        ) as headers,
    ):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response == b"" and headers.call_count == 1
    assert "error_response_failure" in caplog.text and "synthetic-private" not in caplog.text


def test_handler_finish_failure_uses_server_fallback_and_releases_worker(
    server: NoyraHTTPServer, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    original = cast(type[BaseHTTPRequestHandler], server.server.RequestHandlerClass).finish

    def broken(self: Any) -> None:
        original(self)
        raise RuntimeError("synthetic-private during finish")

    with patch.object(server.server.RequestHandlerClass, "finish", new=broken):
        response = raw(server, b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.0 200 ")
    assert "synthetic-private" not in capsys.readouterr().err
    assert "server_failure" in caplog.text


def test_logging_handler_failure_does_not_expose_raw_exception_or_break_response(
    server: NoyraHTTPServer, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = logging.getLogger("noyra.service")

    class BrokenLog(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("synthetic-private logger failure")

    handler = BrokenLog()

    def broken(self: Any) -> None:
        raise RuntimeError("synthetic-private route failure")

    logger.addHandler(handler)
    try:
        with patch.object(server.server.RequestHandlerClass, "do_GET", new=broken):
            response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    finally:
        logger.removeHandler(handler)
    assert response.startswith(b"HTTP/1.0 500 ")
    assert "synthetic-private" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError("synthetic-private disconnect"),
        BrokenPipeError("synthetic-private disconnect"),
        TimeoutError("synthetic-private timeout"),
    ],
)
def test_disconnected_or_timed_out_client_is_closed_without_attempting_new_response(
    server: NoyraHTTPServer, failure: Exception
) -> None:
    def broken(self: Any) -> None:
        raise failure

    with patch.object(server.server.RequestHandlerClass, "do_GET", new=broken):
        response = raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response == b""


@pytest.mark.parametrize(
    ("route", "code"),
    [
        ("/api/participants/login", "runtime_epoch_invalidated"),
        ("/api/admin/project-execution-recovery/release-review", "project_recovery_unavailable"),
        ("/api/admin/lifecycle/pause", "integrity_quarantine"),
        ("/api/admin/migration/cutover", "migration_control_unavailable"),
    ],
)
def test_existing_runtime_failure_contracts_do_not_duplicate_buffered_or_sent_responses(
    server: NoyraHTTPServer, caplog: pytest.LogCaptureFixture, route: str, code: str
) -> None:
    from noyra.core.admission import OperationInvalidated

    handler = server.server.RequestHandlerClass
    caplog.set_level(logging.INFO, logger="noyra.service")
    for phase in ("not_started", "buffered", "sent"):

        def invalidated(self: Any, selected_phase: str = phase) -> None:
            if selected_phase == "sent":
                self._json(200, {"ok": True})
            elif selected_phase == "buffered":
                self.send_response(200)
                self.send_header("Set-Cookie", "synthetic-private=stale")
            raise OperationInvalidated("synthetic-private runtime boundary")

        with (
            patch.object(handler, "_at_rest_available", return_value=True),
            patch.object(server, "allow_mutation", return_value=True),
            patch.object(server, "_is_migration_control", return_value=("migration" in route)),
            patch.object(handler, "_dispatch_post", new=invalidated),
        ):
            response = raw(
                server,
                (f"POST {route} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n").encode(),
            )
        assert response.count(b"HTTP/1.0 ") == 1
        head, body = response.split(b"\r\n\r\n", 1)
        if phase == "sent":
            assert head.startswith(b"HTTP/1.0 200 ") and json.loads(body) == {"ok": True}
            assert "after_headers" in caplog.text
        else:
            assert head.startswith(b"HTTP/1.0 503 ")
            assert json.loads(body) == {"error": code}
            assert b"Set-Cookie" not in head
        assert server.admission.active_operations == 0


def test_mutation_exception_does_not_hold_lease_and_next_request_still_works(
    server: NoyraHTTPServer,
) -> None:
    calls = []

    def broken(self: Any) -> None:
        calls.append(True)
        raise sqlite3.OperationalError("synthetic-private mutation failure")

    handler = server.server.RequestHandlerClass
    with (
        patch.object(handler, "_at_rest_available", return_value=True),
        patch.object(server, "allow_mutation", return_value=True),
        patch.object(handler, "_dispatch_post", new=broken),
    ):
        response = raw(
            server,
            b"POST /api/participants/login HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
    assert response.startswith(b"HTTP/1.0 503 ") and calls == [True]
    assert server.admission.active_operations == 0
    assert raw(server, b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n").startswith(
        b"HTTP/1.0 200 "
    )


def test_failure_before_parser_initializes_fields_uses_explicit_safe_status(
    server: NoyraHTTPServer,
) -> None:
    with patch.object(
        server.server.RequestHandlerClass,
        "parse_request",
        side_effect=RuntimeError("synthetic-private parser failure"),
    ):
        response = raw(server, b"GET /synthetic-private HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.0 500 ")
    assert b"synthetic-private" not in response


def test_sanitized_diagnostic_chain_has_bounded_location_only_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.http_failures import log_http_failure

    captures = []

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captures.append(record)

    logger = logging.getLogger("noyra.service")
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    sink = Sink()
    logger.addHandler(sink)
    try:

        def recurse(depth: int) -> None:
            synthetic_private = "synthetic-private local value"
            if depth:
                recurse(depth - 1)
            raise RuntimeError(synthetic_private)

        try:
            recurse(30)
        except RuntimeError as error:
            log_http_failure(
                error,
                "0" * 32,
                method="GET",
                target="/api/goals/synthetic-private?token=synthetic-private",
            )
    finally:
        logger.removeHandler(sink)
    assert len(captures) == 1
    message = captures[0].getMessage()
    assert "synthetic-private" not in repr(captures[0].__dict__)
    assert "recurse" in message and message.count(" -> ") <= 11
    assert captures[0].exc_info is None and captures[0].stack_info is None


@pytest.mark.parametrize("version", ["", "/v1"])
def test_real_state_route_storage_failure_returns_service_error_and_recovers(
    server: NoyraHTTPServer,
    caplog: pytest.LogCaptureFixture,
    version: str,
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    with patch.object(
        server.projection,
        "state",
        side_effect=sqlite3.DatabaseError("synthetic-private database path"),
    ):
        response = raw(
            server,
            (
                f"GET /api{version}/state?secret=synthetic-private HTTP/1.1\r\n"
                "Host: localhost\r\n\r\n"
            ).encode(),
        )
    assert response.startswith(b"HTTP/1.0 503 ") and b"synthetic-private" not in response
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert payload["request_id"] in caplog.text and "read_public.state" in caplog.text
    assert raw(server, b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n").startswith(
        b"HTTP/1.0 200 "
    )


def test_existing_file_stream_reports_partial_failure_without_appending_json(
    server: NoyraHTTPServer, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="noyra.service")
    file = tmp_path / "synthetic-download.zip"
    file.write_bytes(b"synthetic zip bytes")

    def download(self: Any) -> None:
        self._send_file(file, filename="export.zip", digest="0" * 64, export_job_id=None)

    # stat succeeds and headers are flushed, then the source cannot be opened.
    from pathlib import Path

    original = Path.open

    def unavailable(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == file:
            raise OSError("synthetic-private source unavailable")
        return original(path, *args, **kwargs)

    with (
        patch.object(server.server.RequestHandlerClass, "do_GET", new=download),
        patch.object(Path, "open", new=unavailable),
    ):
        response = raw(
            server,
            b"GET /api/admin/export-jobs/synthetic-private/download HTTP/1.1\r\n"
            b"Host: localhost\r\n\r\n",
        )
    assert response.count(b"HTTP/1.0 ") == 1 and response.startswith(b"HTTP/1.0 200 ")
    assert response.endswith(b"\r\n\r\n")
    assert "after_headers" in caplog.text and "synthetic-private" not in caplog.text


def test_completed_informational_response_still_allows_one_final_error(
    server: NoyraHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cast(type[BaseHTTPRequestHandler], server.server.RequestHandlerClass)
    handler = type("ExpectContinue", (original,), {"protocol_version": "HTTP/1.1"})
    monkeypatch.setattr(server.server, "RequestHandlerClass", handler)

    def broken(self: Any) -> None:
        raise RuntimeError("synthetic-private after informational response")

    with patch.object(handler, "do_POST", new=broken):
        data = raw(
            server,
            b"POST /api/participants/login HTTP/1.1\r\nHost: localhost\r\n"
            b"Expect: 100-continue\r\nContent-Length: 0\r\n\r\n",
        )
    assert data.count(b"HTTP/1.1 100 Continue") == 1
    assert data.count(b"HTTP/1.1 500 ") == 1 and data.count(b"HTTP/1.1 ") == 2
    assert b"synthetic-private" not in data
