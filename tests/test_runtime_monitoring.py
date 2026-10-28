from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from noyra.core.at_rest import AtRestError
from noyra.core.identity import IdentityStore
from noyra.core.types import content_hash
from noyra.monitoring import MONITOR_PATH, load_monitor_token, operational_summary
from noyra.service import NoyraHTTPServer
from test_participant_http import http as base_http
from test_soak_collector import _helper


@pytest.fixture
def http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, base_http).__wrapped__(tmp_path)


def test_monitor_key_only_authorizes_its_local_read_endpoint(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, base = http
    token = owner.monitoring_token
    assert token is not None
    with pytest.raises(HTTPError) as anonymous:
        urlopen(base + MONITOR_PATH, timeout=5)
    assert anonymous.value.code == 401
    headers = {"X-Noyra-Monitor-Token": token.get_secret_value()}
    with urlopen(Request(base + MONITOR_PATH, headers=headers), timeout=5) as response:
        result = json.loads(response.read())
        assert response.headers["Cache-Control"] == "no-store"
    assert result["format"] == "noyra-monitoring/v1"
    assert result["model_activity"]["available"] is True
    assert result["providers"]["available"] is True
    assert result["retention"]["available"] is True
    assert token.get_secret_value() not in json.dumps(result)
    for path in [
        "/api/v1/admin/readiness",
        "/api/v1/diagnostics",
        "/api/v1/config/model-resources",
    ]:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(Request(base + path, headers=headers), timeout=5)
        assert unauthorized.value.code == 401
    with urlopen(
        Request(
            base + MONITOR_PATH,
            headers={"Authorization": "Bearer admin-test-key-with-enough-entropy"},
        ),
        timeout=5,
    ) as response:
        assert response.status == 200


def test_monitor_token_cannot_bypass_real_peer_address(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    from urllib.parse import urlsplit

    from noyra.http_routes.read_admin import monitoring_soak

    owner, _ = http
    assert owner.monitoring_token is not None
    replies: list[tuple[int, Any]] = []
    handler = SimpleNamespace(
        client_address=("198.51.100.9", 1),
        headers={
            "X-Noyra-Monitor-Token": owner.monitoring_token.get_secret_value(),
            "X-Forwarded-For": "127.0.0.1",
        },
        _authorized=lambda _: False,
        _json=lambda code, payload: replies.append((int(code), payload)),
    )
    assert monitoring_soak(cast(Any, handler), owner, urlsplit(MONITOR_PATH))
    assert replies == [(401, {"error": "unauthorized"})]


@pytest.mark.parametrize(
    "state",
    [
        [],
        {"secret": "private"},
        {"since": 1},
        {"since": "2026-10-27"},
        {"activity_cursors": []},
        {"activity_cursors": {"calls": {"id": "missing-time"}}},
        {"activity_cursors": {"calls": {"id": "call", "timestamp": "invalid"}}},
    ],
)
def test_monitor_rejects_invalid_bounded_state(
    http: tuple[NoyraHTTPServer, str],
    state: Any,
) -> None:
    owner, base = http
    assert owner.monitoring_token is not None
    request = Request(
        base + MONITOR_PATH + "?" + urlencode({"state": json.dumps(state)}),
        headers={"X-Noyra-Monitor-Token": owner.monitoring_token.get_secret_value()},
    )
    with pytest.raises(HTTPError) as invalid:
        urlopen(request, timeout=5)
    assert invalid.value.code == 400


def test_collector_uses_runtime_without_opening_production_sqlite(
    http: tuple[NoyraHTTPServer, str],
    tmp_path: Path,
) -> None:
    owner, base = http
    helper = _helper()
    with (
        patch.object(
            helper, "_systemd", return_value={"ActiveState": "active", "SubState": "running"}
        ),
        patch.object(helper, "_model_activity", side_effect=AssertionError("must not open sqlite")),
        patch.object(
            helper, "_provider_health", side_effect=AssertionError("must not open sqlite")
        ),
        patch.object(helper, "_retention", side_effect=AssertionError("must not open sqlite")),
    ):
        snapshot = helper.collect_snapshot(
            data_dir=owner.settings.data_dir,
            backup_dir=tmp_path / "backups",
            health_base=base,
            use_runtime_monitor=True,
        )
    assert snapshot["monitoring"] == {"source": "runtime", "runtime_error": None}
    assert snapshot["model_activity"]["available"] is True
    assert snapshot["providers"]["available"] is True
    assert snapshot["health"]["ready"]["status"] == 200


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "http://localhost",
        "http://127.0.0.1@evil.test",
        "http://127.0.0.1/path",
    ],
)
def test_monitor_collector_never_sends_credentials_off_origin(tmp_path: Path, origin: str) -> None:
    helper = _helper()
    with patch.object(helper, "build_opener", side_effect=AssertionError("must not request")):
        result = helper._local_monitoring(tmp_path, origin, since=None, cursors=None)
    assert result["available"] is False


def test_monitor_token_is_stable_private_and_rejects_symlink(tmp_path: Path) -> None:
    token = load_monitor_token(tmp_path)
    assert load_monitor_token(tmp_path).get_secret_value() == token.get_secret_value()
    path = tmp_path / "cache" / "monitoring" / "read-token"
    assert len(path.read_bytes()) == 64
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    # Use the existing cross-platform reparse validator, independently of the OS.
    with (
        patch("noyra.monitoring.validate_private_file", side_effect=ValueError("linked path")),
        pytest.raises(ValueError, match="linked path"),
    ):
        load_monitor_token(tmp_path)


def test_monitor_summary_is_subject_scoped_and_sees_active_wal(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = http
    db = owner.kernel.database
    IdentityStore(db).ensure("other-monitor-subject", content_hash("other-monitor-subject"))
    # Keep a connection open so the committed call lives in an active WAL.
    with db.connection() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        now = "2026-10-27T00:00:00+00:00"
        for identifier, subject in [
            ("own-call", owner.kernel.subject_id),
            ("foreign-private-call", "other-monitor-subject"),
        ]:
            writer.execute(
                "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,request_hash,"
                "idempotency_key,status,usage_estimated,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,'succeeded',0,?,?)",
                (
                    identifier,
                    subject,
                    "private-provider",
                    "private-model",
                    "private-purpose",
                    "hash",
                    identifier,
                    now,
                    now,
                ),
            )
        writer.commit()
        result = operational_summary(
            db.path, owner.kernel.subject_id, since="2026-10-26T00:00:00+00:00"
        )
    assert result["model_activity"]["calls"]["rows"] == 1
    assert result["activity_cursors"]["calls"]["id"] == "own-call"
    for private in ["foreign-private-call", "private-provider", "private-model", "private-purpose"]:
        assert private not in json.dumps(result)


def test_readiness_failure_exposes_safe_reason_to_collector(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, base = http
    helper = _helper()
    with patch.object(owner, "_current_storage", side_effect=RuntimeError("private details")):
        result = helper._health(base + "/health/ready")
    assert result == {
        "status": 503,
        "body": {"status": "degraded", "service": "noyra", "reason_code": "readiness_check_failed"},
    }
    assert "private details" not in json.dumps(result)


@pytest.mark.parametrize("target", ["redirect", "proxy"])
def test_monitor_collector_ignores_proxy_and_refuses_redirects(
    http: tuple[NoyraHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    owner, base = http
    helper = _helper()
    requests: list[str | None] = []

    class RedirectServer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.headers.get("X-Noyra-Monitor-Token"))
            self.send_response(302)
            self.send_header("Location", base + MONITOR_PATH)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        other = f"http://127.0.0.1:{server.server_address[1]}"
        if target == "proxy":
            monkeypatch.setenv("http_proxy", other)
            monkeypatch.setenv("HTTP_PROXY", other)
            monkeypatch.setenv("no_proxy", "")
            monkeypatch.setenv("NO_PROXY", "")
            result = helper._local_monitoring(
                owner.settings.data_dir, base, since=None, cursors=None
            )
            assert result["format"] == "noyra-monitoring/v1"
            assert requests == []
        else:
            result = helper._local_monitoring(
                owner.settings.data_dir, other, since=None, cursors=None
            )
            assert result["available"] is False and result["status"] == 302
            assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_monitor_cursor_and_inflight_completion_keep_exact_counts(
    http: tuple[NoyraHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.monitoring as monitoring

    owner, _ = http
    database = owner.kernel.database
    with database.transaction() as connection:
        for index, completed in [(0, True), (1, True), (2, False)]:
            connection.execute(
                "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,request_hash,"
                "idempotency_key,status,usage_estimated,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (
                    f"monitor-call-{index}",
                    owner.kernel.subject_id,
                    "provider",
                    "model",
                    "purpose",
                    "hash",
                    f"monitor-idem-{index}",
                    "succeeded" if completed else "prepared",
                    "2026-10-27T00:00:00+00:00",
                    "2026-10-27T00:00:01+00:00" if completed else None,
                ),
            )
    monkeypatch.setattr(monitoring, "MAX_ACTIVITY_ROWS", 1)
    first = operational_summary(
        database.path, owner.kernel.subject_id, since="2026-10-26T00:00:00+00:00"
    )
    assert first["model_activity"]["calls"]["rows"] == 1
    assert first["model_activity"]["in_progress"]["calls"] == 1
    second = operational_summary(
        database.path, owner.kernel.subject_id, cursors=first["activity_cursors"]
    )
    assert second["model_activity"]["calls"]["rows"] == 1
    assert second["activity_cursors"]["calls"]["id"] != first["activity_cursors"]["calls"]["id"]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET status='succeeded',completed_at=? WHERE call_id=?",
            ("2026-10-27T00:00:02+00:00", "monitor-call-2"),
        )
    third = operational_summary(
        database.path, owner.kernel.subject_id, cursors=second["activity_cursors"]
    )
    assert third["model_activity"]["calls"]["rows"] == 1
    assert third["model_activity"]["in_progress"]["calls"] == 0
    assert third["activity_cursors"]["calls"]["id"] == "monitor-call-2"


@pytest.mark.skipif(
    __import__("sys").platform != "linux" or __import__("os").geteuid() != 0,
    reason="requires a Linux root mount namespace",
)
def test_monitor_collects_through_an_actual_read_only_mount(
    http: tuple[NoyraHTTPServer, str],
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    owner, base = http
    script = r"""
import importlib.util, json, pathlib, sqlite3, subprocess, sys
root=pathlib.Path(sys.argv[1]); base=sys.argv[2]; collector=pathlib.Path(sys.argv[3])
subprocess.run(['mount','--bind',str(root),str(root)],check=True)
subprocess.run(['mount','-o','remount,bind,ro',str(root)],check=True)
p=root/'noyra.sqlite3'
assert not p.with_name(p.name+'-wal').exists()
try:
    c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    try:c.execute('SELECT COUNT(*) FROM model_calls').fetchone()
    finally:c.close()
except sqlite3.OperationalError as e:
    assert e.sqlite_errorname=='SQLITE_CANTOPEN'
else:
    raise AssertionError('expected the original read-only WAL failure')
spec=importlib.util.spec_from_file_location('collector',collector)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
snapshot=module.collect_snapshot(data_dir=root,backup_dir=root/'absent',health_base=base,use_runtime_monitor=True)
assert snapshot['monitoring']['source']=='runtime', snapshot
assert snapshot['model_activity']['available'] is True, snapshot
assert snapshot['providers']['available'] is True, snapshot
assert snapshot['retention']['available'] is True, snapshot
print(json.dumps({'read_only_sandbox':'passed','monitoring':snapshot['monitoring']}))
"""
    collector = Path(__file__).resolve().parents[1] / "scripts/noyra-soak-collector.py"
    result = subprocess.run(
        [
            "unshare",
            "--mount",
            "--propagation",
            "private",
            sys.executable,
            "-c",
            script,
            str(owner.settings.data_dir),
            base,
            str(collector),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unavailable_monitor_credential_does_not_block_owned_runtime(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = http
    second = NoyraHTTPServer(owner.kernel, owner.settings)
    try:
        with patch("noyra.monitoring.load_monitor_token", side_effect=ValueError("invalid token")):
            second.start()
        base = f"http://127.0.0.1:{second.address[1]}"
        assert second.monitoring_token is None
        with urlopen(base + "/health/live", timeout=5) as response:
            assert response.status == 200
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(base + MONITOR_PATH, headers={"X-Noyra-Monitor-Token": ""}), timeout=5)
        assert denied.value.code == 401
    finally:
        second.close()


def test_monitor_token_rejects_hardlinked_credential(tmp_path: Path) -> None:
    import os

    load_monitor_token(tmp_path)
    path = tmp_path / "cache" / "monitoring" / "read-token"
    os.link(path, path.with_name("linked-token"))
    with pytest.raises(ValueError, match="non-hard-linked"):
        load_monitor_token(tmp_path)


@pytest.mark.skipif(__import__("os").name == "nt", reason="requires POSIX symlink permissions")
def test_monitor_token_rejects_an_actual_symlink(tmp_path: Path) -> None:
    load_monitor_token(tmp_path)
    path = tmp_path / "cache" / "monitoring" / "read-token"
    external = tmp_path / "outside-token"
    path.rename(external)
    path.symlink_to(external)
    with pytest.raises(AtRestError, match="symlink"):
        load_monitor_token(tmp_path)
