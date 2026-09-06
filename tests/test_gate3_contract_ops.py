from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import urlopen

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


def test_global_error_contract_contains_boundary_statuses() -> None:
    routes = route_contracts()
    assert any(route.path == "/health/live" for route in routes)
    assert any(route.path == "/health/ready" for route in routes)
    for route in routes:
        if route.path.startswith("/api/v1/"):
            assert {429, 503} <= set(route.effective_responses)
        if route.method == "POST" and route.request_json:
            assert {411, 413} <= set(route.effective_responses)
