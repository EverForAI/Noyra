from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr, ValidationError

from noyra.core.types import content_hash
from noyra.service import NoyraService, ServiceSettings
from noyra.service_contract import route_contracts


def _settings(tmp_path: Path, **overrides: object) -> ServiceSettings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "subject_id": "Noyra-gate3-ops",
        "genesis_hash": content_hash({"gate": 3}),
        "host": "127.0.0.1",
        "port": 0,
        "integrity_mode": "off",
    }
    values.update(overrides)
    return ServiceSettings(**cast(Any, values))


def test_role_tokens_reject_placeholders_and_duplicates(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="non-placeholder"):
        _settings(
            tmp_path,
            read_token=SecretStr("replace-with-at-least-32-random-characters"),
        )
    token = "x" * 40
    with pytest.raises(ValidationError, match="distinct"):
        _settings(
            tmp_path,
            read_token=SecretStr(token),
            operator_token=SecretStr(token),
        )


def test_non_loopback_listener_requires_authentication(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="at least one bearer token"):
        _settings(tmp_path, host="0.0.0.0", allow_insecure_non_loopback=True)


def test_liveness_and_readiness_are_separate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"status": "ok", "service": "noyra"}
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["service"] == "noyra"
    finally:
        service.http.close()
        service.kernel.close()


def test_target_readiness_binds_subject_and_migration_target_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_MIGRATION_TARGET_ID", "shelter-1")
    token = "readiness-operator-token-with-enough-entropy"
    settings = _settings(tmp_path, operator_token=SecretStr(token))
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        request = Request(
            f"http://127.0.0.1:{port}/api/v1/admin/readiness",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["subject_id"] == settings.subject_id
        assert payload["migration_target_id"] == "shelter-1"
    finally:
        service.http.close()
        service.kernel.close()


def test_health_readiness_is_cached_and_rate_errors_advertise_retry_after(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, health_cache_ttl_seconds=30)
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            first = json.loads(response.read())
        assert service.http._cached_health() is not None
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            second = json.loads(response.read())
        assert second == first

        for _ in range(settings.request_rate_limit_per_minute):
            service.http.allow_request("127.0.0.1")
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5)
        assert error.value.code == 429
        assert error.value.headers.get("Retry-After") == "60"
    finally:
        service.http.close()
        service.kernel.close()


def test_gate3_deployment_restart_and_log_limits_are_bounded() -> None:
    systemd = (Path(__file__).parents[1] / "deploy" / "systemd" / "noyra.service").read_text()
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    assert "Restart=on-failure" in systemd
    assert "RestartPreventExitStatus=78" in systemd
    assert "StartLimitBurst=5" in systemd
    assert "LogRateLimitBurst=1000" in systemd
    assert "restart: on-failure:5" in compose
    assert 'max-size: "10m"' in compose
    assert 'max-file: "3"' in compose
    assert "NOYRA_PROFILE: production" in compose
    assert "NOYRA_DEPLOYMENT_PROFILE: container_internal" in compose
    assert '"127.0.0.1:8765:8765"' in compose


def test_global_error_contract_contains_boundary_statuses() -> None:
    routes = route_contracts()
    assert any(route.path == "/health/live" for route in routes)
    assert any(route.path == "/health/ready" for route in routes)
    for route in routes:
        if route.path.startswith("/api/v1/"):
            assert {429, 503} <= set(route.effective_responses)
        if route.method == "POST" and route.request_json:
            assert {411, 413} <= set(route.effective_responses)


def test_public_health_does_not_leak_cached_admin_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    token = "health-projection-operator-token-123456789"
    settings = _settings(tmp_path, operator_token=SecretStr(token), health_cache_ttl_seconds=30)
    service = NoyraService(settings)
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        private = {
            "enforced": False,
            "ready": True,
            "volume": {"volume_id": "private-volume"},
            "backup_key": {"active_key_fingerprint": "private-fingerprint"},
        }
        monkeypatch.setattr(service.http, "at_rest", SimpleNamespace(health=lambda: private))
        for path in ("/health/ready", "/health"):
            request = Request(
                f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"}
            )
            with urlopen(request, timeout=5) as response:
                authenticated = json.loads(response.read())
            if path == "/health":
                assert authenticated["at_rest"] == private
            for headers in ({}, {"Authorization": "Bearer incorrect"}):
                request = Request(f"http://127.0.0.1:{port}{path}", headers=headers)
                with urlopen(request, timeout=5) as response:
                    assert json.loads(response.read()) == {"status": "ok", "service": "noyra"}
        for path in ("/api/admin/readiness", "/api/v1/admin/readiness"):
            with pytest.raises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
            assert error.value.code == 401
            request = Request(
                f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"}
            )
            with urlopen(request, timeout=5) as response:
                assert json.loads(response.read())["at_rest"] == private
        monkeypatch.setattr(service.http, "quarantine_checker", lambda: True)
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=5)
        assert error.value.code == 503
        assert json.loads(error.value.read()) == {"status": "degraded", "service": "noyra"}
    finally:
        service.http.close()
        service.kernel.close()


def test_root_activation_uses_authenticated_readiness_with_managed_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from noyra.migration import activation

    # CI need not have an installed noyra service account; model its ownership
    # with the current test user while retaining real file-mode checks.
    monkeypatch.setattr(activation, "_service_uid", lambda: getattr(os, "getuid", lambda: 0)())
    monkeypatch.setattr(activation, "_service_gid", lambda: getattr(os, "getgid", lambda: 0)())
    token = "activation-readiness-token-with-entropy-12345"
    token_path = tmp_path / "operator.token"
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    monkeypatch.setenv("NOYRA_OPERATOR_TOKEN_FILE", str(token_path))
    monkeypatch.delenv("NOYRA_OPERATOR_TOKEN_CREDENTIAL", raising=False)
    monkeypatch.setenv("NOYRA_MIGRATION_TARGET_ID", "shelter-1")
    service = NoyraService(_settings(tmp_path, operator_token=SecretStr(token)))
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        monkeypatch.setattr(activation, "_configured_port", lambda _: port)
        # Transport and API authentication are real; only systemctl is stubbed.
        systemd = activation._Systemd(tmp_path)
        monkeypatch.setattr(systemd, "is_active", lambda: True)
        assert systemd.wait_ready(service.kernel.subject_id, "shelter-1", 2)
        assert not systemd.wait_ready(service.kernel.subject_id, "wrong-target", 0.1)
        token_path.write_text("invalid-token", encoding="utf-8")
        assert not systemd.wait_ready(service.kernel.subject_id, "shelter-1", 0.1)
        token_path.unlink()
        assert not systemd.wait_ready(service.kernel.subject_id, "shelter-1", 0.1)
        credentials = tmp_path / "credentials"
        credentials.mkdir()
        key = credentials / "operator"
        key.write_text(token, encoding="utf-8")
        key.chmod(0o600)
        monkeypatch.delenv("NOYRA_OPERATOR_TOKEN_FILE")
        monkeypatch.setenv("NOYRA_OPERATOR_TOKEN_CREDENTIAL", "operator")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
        assert systemd.wait_ready(service.kernel.subject_id, "shelter-1", 2)
    finally:
        service.http.close()
        service.kernel.close()


def test_root_readiness_never_follows_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from noyra.migration import activation

    visited: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/capture-token")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        monkeypatch.delenv("NOYRA_OPERATOR_TOKEN_FILE", raising=False)
        monkeypatch.delenv("NOYRA_OPERATOR_TOKEN_CREDENTIAL", raising=False)
        monkeypatch.setenv("NOYRA_OPERATOR_TOKEN", "root-readiness-test-token")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setattr(activation, "_configured_port", lambda _: server.server_port)
        systemd = activation._Systemd(tmp_path)
        monkeypatch.setattr(systemd, "is_active", lambda: True)
        assert not systemd.wait_ready("subject", "target", 0.1)
        assert visited == ["/api/v1/admin/readiness"]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
