from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings

TOKENS = {
    role: f"synthetic-{role}-" + "x" * 40
    for role in ("admin", "operator", "read", "export", "break_glass")
}


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NoyraHTTPServer]:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="auth-input-contract",
        genesis_hash=content_hash("auth-input-contract"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(TOKENS["admin"]),
        operator_token=SecretStr(TOKENS["operator"]),
        read_token=SecretStr(TOKENS["read"]),
        export_token=SecretStr(TOKENS["export"]),
        break_glass_token=SecretStr(TOKENS["break_glass"]),
        admin_login_rate_limit_per_minute=20,
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    http = NoyraHTTPServer(kernel, settings)
    handle_error = Mock(wraps=http.server.handle_error)
    monkeypatch.setattr(http.server, "handle_error", handle_error)
    http.start()
    try:
        yield http
        assert handle_error.call_count == 0
    finally:
        http.close()
        kernel.close()


def call(
    server: NoyraHTTPServer,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    req = Request(
        f"http://127.0.0.1:{server.address[1]}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        body = response.read()
        return (
            response.status,
            json.loads(body) if body else {},
            response.headers.get("Set-Cookie", ""),
        )


@pytest.mark.parametrize(
    "token",
    [
        None,
        123,
        True,
        [],
        {},
        "",
        "wrong",
        "令牌",
        "\uff21\uff22\uff23",
        "café",
        "\ud800",
        " " + TOKENS["operator"] + " ",
    ],
    ids=[
        "null",
        "number",
        "bool",
        "array",
        "object",
        "empty",
        "wrong",
        "chinese",
        "fullwidth",
        "latin1",
        "surrogate",
        "spaces",
    ],
)
def test_malformed_login_returns_401_and_preserves_health(
    server: NoyraHTTPServer,
    token: Any,
) -> None:
    status, body, cookie = call(server, "/admin/session", payload={"token": token})
    assert (status, body) == (401, {"error": "unauthorized"})
    assert not cookie
    assert call(server, "/health/live")[0] == 200


@pytest.mark.parametrize("role", list(TOKENS))
def test_ascii_login_roles_are_unchanged(server: NoyraHTTPServer, role: str) -> None:
    status, body, cookie = call(server, "/admin/session", payload={"token": TOKENS[role]})
    if role in {"admin", "operator"}:
        assert status == 200 and body["role"] == role
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    elif role == "break_glass":
        assert (status, body) == (403, {"error": "break_glass_session_forbidden"})
        assert not cookie
    else:
        assert (status, body) == (401, {"error": "unauthorized"})
        assert not cookie


def test_non_ascii_bearer_and_csrf_reject_without_losing_valid_cookie_session(
    server: NoyraHTTPServer,
) -> None:
    malformed = {"Authorization": "Bearer café"}
    assert call(server, "/api/admin/conversations", headers=malformed)[:2] == (
        401,
        {"error": "unauthorized"},
    )
    status, login, cookie = call(
        server,
        "/admin/session",
        payload={"token": TOKENS["operator"]},
    )
    assert status == 200
    headers = {**malformed, "Cookie": cookie.split(";", 1)[0]}
    assert call(server, "/api/admin/conversations", headers=headers)[0] == 200
    assert (
        call(
            server,
            "/api/admin/conversations",
            payload={"title": "blocked"},
            headers={**headers, "X-CSRF-Token": "café"},
        )[0]
        == 401
    )
    assert call(
        server, "/admin/session/logout", payload={}, headers={**headers, "X-CSRF-Token": "café"}
    )[:2] == (
        401,
        {"error": "csrf_required"},
    )
    assert call(server, "/admin/session", headers=headers)[1]["authenticated"]
    assert (
        call(
            server,
            "/api/admin/conversations",
            payload={"title": "allowed"},
            headers={**headers, "X-CSRF-Token": login["csrf_token"]},
        )[0]
        == 200
    )
    assert (
        call(
            server,
            "/admin/session/logout",
            payload={},
            headers={**headers, "X-CSRF-Token": login["csrf_token"]},
        )[0]
        == 204
    )
    assert not call(server, "/admin/session", headers=headers)[1]["authenticated"]


def test_non_ascii_login_consumes_the_same_durable_failure_budget(
    server: NoyraHTTPServer,
) -> None:
    server.settings = server.settings.model_copy(update={"admin_login_rate_limit_per_minute": 2})
    for _ in range(2):
        assert call(server, "/admin/session", payload={"token": "令牌"})[0] == 401
    assert call(server, "/admin/session", payload={"token": TOKENS["operator"]})[:2] == (
        429,
        {"error": "admin_login_rate_limited"},
    )
