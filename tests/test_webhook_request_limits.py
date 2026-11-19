from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from http import HTTPStatus
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.http_transport import handler_type
from noyra.interaction import TransportInput
from noyra.service import NoyraHTTPServer, ServiceSettings

SECRET = "synthetic-request-limit-secret"
PREFIXES = ("/webhooks/", "/api/v1/webhooks/")


@pytest.mark.parametrize("maximum", [100_000, 2_000_000])
def test_route_limit_closes_before_reading_or_authenticating(maximum: int) -> None:
    owner = cast(
        Any,
        SimpleNamespace(
            settings=SimpleNamespace(max_request_bytes=maximum),
            allow_mutation=lambda path: True,
            transports=Mock(),
        ),
    )
    handler_class = handler_type(
        owner, web_root=Path(__file__).parent, public_article_html=lambda *a, **kw: b""
    )
    handler = cast(Any, object.__new__(handler_class))
    handler._at_rest_available = lambda: True
    handler.path = "/webhooks/qq/synthetic"
    handler.headers = {"Content-Length": str(min(maximum, 1_048_576) + 1)}
    handler.close_connection = False
    handler.rfile = Mock(wraps=BytesIO(b"unread callback and following request"))
    responses: list[tuple[int, dict[str, str]]] = []
    handler._json = lambda status, payload: responses.append((int(status), payload))

    handler._receive_inbound_webhook()

    assert responses == [(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})]
    assert handler.close_connection is True
    handler.rfile.read.assert_not_called()
    owner.transports.get.assert_not_called()


@pytest.fixture(scope="module", params=[100_000, 2_000_000])
def callback_server(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[tuple[NoyraHTTPServer, str, int]]:
    maximum = int(request.param)
    root = tmp_path_factory.mktemp("webhook-limits")
    subject = f"request-limit-{maximum}"
    genesis = content_hash(subject)
    kernel = SubjectKernel(root / "noyra.sqlite3", subject, genesis)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(
        kernel,
        ServiceSettings(
            data_dir=root,
            subject_id=subject,
            genesis_hash=genesis,
            host="127.0.0.1",
            port=0,
            max_request_bytes=maximum,
            integrity_mode="off",
        ),
    )
    try:
        transport = server.transports.configure(
            subject,
            TransportInput(
                channel="qq",
                label="Synthetic request limit",
                endpoint="https://api.sgroup.qq.com",
                credentials={
                    "app_id": SecretStr("synthetic-app"),
                    "app_secret": SecretStr(SECRET),
                },
            ),
            actor="operator",
        )
        server.start()
        yield server, transport.transport_id, min(maximum, 1_048_576)
    finally:
        server.close()
        kernel.close()


@pytest.mark.parametrize("prefix", PREFIXES)
def test_oversized_callback_closes_keepalive_without_processing_following_request(
    callback_server: tuple[NoyraHTTPServer, str, int], prefix: str
) -> None:
    server, transport_id, limit = callback_server
    with socket.create_connection(server.address, timeout=10) as client:
        client.sendall(
            (
                f"POST {prefix}qq/{transport_id} HTTP/1.1\r\n"
                f"Host: localhost\r\nContent-Length: {limit + 1}\r\n"
                "Connection: keep-alive\r\n\r\n"
                "{}GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n"
            ).encode("ascii")
        )
        response = bytearray()
        while block := client.recv(8192):
            response.extend(block)
    assert b"HTTP/1.0 413" in response
    assert b"Connection: close" in response
    assert response.count(b"HTTP/1.0 ") == 1
    assert json.loads(response.split(b"\r\n\r\n", 1)[1]) == {"error": "too_large"}


@pytest.mark.parametrize("prefix", PREFIXES)
def test_signed_qq_challenge_at_effective_limit_remains_accepted(
    callback_server: tuple[NoyraHTTPServer, str, int], prefix: str
) -> None:
    server, transport_id, limit = callback_server
    timestamp = str(int(time.time()))
    payload = json.dumps(
        {"op": 13, "d": {"plain_token": "synthetic-limit", "event_ts": timestamp}}
    ).encode("ascii")
    body = payload + b" " * (limit - len(payload))
    key = Ed25519PrivateKey.from_private_bytes((SECRET.encode() * 32)[:32])
    signature = key.sign(timestamp.encode("ascii") + body).hex()
    connection = HTTPConnection(*server.address, timeout=15)
    try:
        connection.request(
            "POST",
            f"{prefix}qq/{transport_id}",
            body,
            {
                "Content-Type": "application/json",
                "X-Signature-Timestamp": timestamp,
                "X-Signature-Ed25519": signature,
            },
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result["plain_token"] == "synthetic-limit"
        key.public_key().verify(
            bytes.fromhex(result["signature"]), (timestamp + "synthetic-limit").encode()
        )
    finally:
        connection.close()
    with server.kernel.database.connection() as database:
        assert (
            database.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 0
        )
