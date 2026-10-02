from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, NoyraService, ServiceSettings


def _service(data_dir: Path, *, rate_limit: int = 2) -> NoyraService:
    service = NoyraService(
        ServiceSettings(
            data_dir=data_dir,
            subject_id="Noyra-durable-admin",
            genesis_hash=content_hash({"test": "durable-admin"}),
            host="127.0.0.1",
            port=0,
            operator_token=SecretStr("operator-" + "o" * 40),
            admin_login_rate_limit_per_minute=rate_limit,
            integrity_mode="off",
        )
    )
    service.boot()
    return service


def test_admin_login_failure_budget_survives_service_restart(tmp_path: Path) -> None:
    first = _service(tmp_path / "service")
    try:
        assert first.http.allow_admin_login("198.51.100.24") is True
        assert first.http.allow_admin_login("198.51.100.24") is True
    finally:
        first.close()

    restarted = _service(tmp_path / "service")
    try:
        assert restarted.http.allow_admin_login("198.51.100.24") is False
    finally:
        restarted.close()


def test_admin_session_survives_restart_and_revocation_is_durable(tmp_path: Path) -> None:
    first = _service(tmp_path / "service", rate_limit=10)
    session_id, created = first.http.create_admin_session(role="operator", actor="web-operator")
    try:
        with first.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT session_hash, state_hash FROM admin_sessions"
            ).fetchone()
            assert row is not None
            assert row["session_hash"] != session_id
            assert row["state_hash"]
    finally:
        first.close()

    restarted = _service(tmp_path / "service", rate_limit=10)
    try:
        restored = restarted.http.admin_session(session_id)
        assert restored is not None
        assert restored.role == created.role
        assert restored.actor == created.actor
        assert restored.csrf_token == created.csrf_token
        restarted.http.revoke_admin_session(session_id)
    finally:
        restarted.close()

    final = _service(tmp_path / "service", rate_limit=10)
    try:
        assert final.http.admin_session(session_id) is None
    finally:
        final.close()


def test_admin_login_rate_cleanup_removes_expired_events(tmp_path: Path) -> None:
    service = _service(tmp_path / "service", rate_limit=1)
    try:
        with service.kernel.database.transaction() as connection:
            connection.execute("UPDATE admin_login_rate_events SET occurred_at = 0")
        assert service.http.allow_admin_login("203.0.113.10") is True
    finally:
        service.close()


def test_admin_control_tables_are_shared_by_independent_http_servers(tmp_path: Path) -> None:
    service = _service(tmp_path / "service", rate_limit=2)
    second = NoyraHTTPServer(service.kernel, service.settings)
    try:
        assert service.http.allow_admin_login("203.0.113.11") is True
        assert second.allow_admin_login("203.0.113.11") is True
        assert second.allow_admin_login("203.0.113.11") is False
    finally:
        second.close()
        service.close()
