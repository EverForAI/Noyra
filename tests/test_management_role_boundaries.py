from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from noyra.core import EventStore
from noyra.service import NoyraHTTPServer
from test_authentication_input_contract import TOKENS, call
from test_authentication_input_contract import server as server


def authorization(role: str) -> dict[str, str]:
    if role == "anonymous":
        return {}
    return {
        "Authorization": "Bearer " + TOKENS.get(role, "synthetic-unconfigured-token-" + "z" * 40)
    }


@pytest.mark.parametrize("role", [*TOKENS, "anonymous", "unknown"])
def test_bearer_capabilities_distinguish_reads_operator_and_export_routes(
    server: NoyraHTTPServer, role: str
) -> None:
    headers = authorization(role)
    conversation = server.conversations.create(server.kernel.subject_id, "M07 private conversation")
    classes = (
        (
            set(TOKENS),
            [
                "state/details",
                "admin/conversations",
                f"admin/conversations/{conversation['conversation_id']}/messages",
                "admin/lifecycle-summary",
                "admin/usage",
                "deliveries",
            ],
        ),
        (
            {"operator", "admin", "break_glass"},
            [
                "config/training-policy",
                "config/transports",
                "config/transports/outreach",
                "admin/migration/policy",
            ],
        ),
        ({"export", "admin", "break_glass"}, ["admin/export-jobs"]),
    )
    for prefix in ("/api/", "/api/v1/"):
        for permitted, routes in classes:
            for route in routes:
                status, body, _cookie = call(server, prefix + route, headers=headers)
                assert status == (200 if role in permitted else 401), (role, prefix, route, body)
        for suffix in ("", "/download"):
            status, body, _cookie = call(
                server, prefix + "admin/export-jobs/missing" + suffix, headers=headers
            )
            assert status == (404 if role in {"export", "admin", "break_glass"} else 401)
        assert call(server, prefix + "state", headers=headers)[0] == 200


@pytest.mark.parametrize("role", [*TOKENS, "anonymous", "unknown"])
def test_read_and_export_tokens_cannot_write_operator_conversations(
    server: NoyraHTTPServer, role: str
) -> None:
    subject = server.kernel.subject_id
    before = server.conversations.list(subject)["items"]
    allowed = role in {"operator", "admin", "break_glass"}
    for prefix in ("/api/", "/api/v1/"):
        with patch.object(
            server.conversations, "create", wraps=server.conversations.create
        ) as create:
            status, body, _cookie = call(
                server,
                prefix + "admin/conversations",
                payload={"title": f"M07 {role} write"},
                headers=authorization(role),
            )
            assert status == (200 if allowed else 401), body
            assert create.call_count == int(allowed)
    after = server.conversations.list(subject)["items"]
    assert len(after) == len(before) + (2 if allowed else 0)


def test_operator_outreach_authorization_requires_opt_in_and_rejects_stale_updates(
    server: NoyraHTTPServer,
) -> None:
    from noyra.interaction import TransportInput

    record = server.transports.configure(
        server.kernel.subject_id,
        TransportInput(channel="email", label="Synthetic mail", endpoint="smtps://mail.example"),
        actor="operator",
    )
    path = f"/api/config/transports/{record.transport_id}/outreach"
    operator = authorization("operator")
    payload: dict[str, Any] = {
        "operation": "authorize",
        "recipient": "test@EXAMPLE.COM",
        "reason": "opt-in",
        "operator_name": "Synthetic operator",
        "opt_out_email": "stop@example.com",
    }
    assert call(server, path, payload=payload, headers=operator)[0] == 400
    payload["confirm_opt_in"] = True
    for role in ("read", "export", "anonymous"):
        assert call(server, path, payload=payload, headers=authorization(role))[0] == 401
    status, entry, _ = call(server, path, payload=payload, headers=operator)
    assert status == 200 and entry["active"] is True
    status, inventory, _ = call(server, "/api/config/transports/outreach", headers=operator)
    assert status == 200 and isinstance(inventory, list)
    assert inventory[0]["eligible_recipients"] == 1
    assert "@" not in json.dumps(inventory)
    status, entry, _ = call(
        server,
        path,
        headers=operator,
        payload={"operation": "revoke", "recipient": "test@example.com", "reason": "opt-out"},
    )
    assert status == 200 and entry["active"] is False
    assert call(server, path, payload=payload, headers=operator)[0] == 409


@pytest.mark.parametrize("role", list(TOKENS))
def test_export_role_does_not_override_runtime_or_training_release_controls(
    server: NoyraHTTPServer, role: str
) -> None:
    server.settings = server.settings.model_copy(update={"developer_log_export_enabled": False})
    policy = server.training.policy(server.kernel.subject_id)
    status, _policy, _cookie = call(
        server,
        "/api/v1/config/training-policy",
        payload={"expected_version": policy.policy_version, "export_enabled": False},
        headers=authorization("admin"),
    )
    assert status == 200
    assert not server.training.policy(server.kernel.subject_id).export_enabled
    allowed = role in {"export", "admin", "break_glass"}
    with patch.object(server.export_jobs, "create", wraps=server.export_jobs.create) as create:
        for prefix in ("/api/", "/api/v1/"):
            for kind, denial in (("runtime", 404), ("training", 403)):
                for path, payload in (
                    (prefix + "admin/export-jobs", {"kind": kind}),
                    (prefix + f"admin/{kind}-export", None),
                ):
                    status, body, _cookie = call(
                        server, path, payload=payload, headers=authorization(role)
                    )
                    assert status == (denial if allowed else 401), (role, path, body)
        create.assert_not_called()
    # Disabling artifact creation does not revoke the token's existing read capability.
    assert call(server, "/api/v1/state/details", headers=authorization(role))[0] == 200


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_browser_sessions_keep_read_and_csrf_writes_without_bearer_export_rights(
    server: NoyraHTTPServer, role: str
) -> None:
    status, session, cookie = call(server, "/admin/session", payload={"token": TOKENS[role]})
    assert status == 200
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    headers = {"Cookie": cookie.split(";", 1)[0]}
    assert call(server, "/api/v1/state/details", headers=headers)[0] == 200
    assert call(server, "/api/v1/config/training-policy", headers=headers)[0] == 200
    payload = {"title": "M07 session write"}
    with patch.object(server.conversations, "create", wraps=server.conversations.create) as create:
        assert call(server, "/api/admin/conversations", headers=headers, payload=payload)[0] == 401
        create.assert_not_called()
        headers["X-CSRF-Token"] = session["csrf_token"]
        assert call(server, "/api/admin/conversations", headers=headers, payload=payload)[0] == 200
    with patch.object(server.export_jobs, "create", wraps=server.export_jobs.create) as create:
        for path, export_payload in (
            ("/api/v1/admin/export-jobs", None),
            ("/api/v1/admin/export-jobs", {"kind": "runtime"}),
            ("/api/admin/runtime-export", None),
        ):
            assert call(server, path, headers=headers, payload=export_payload)[0] == 401
        create.assert_not_called()


def test_break_glass_stays_bearer_only_and_records_rejected_session(
    server: NoyraHTTPServer,
) -> None:
    with patch.object(server, "create_admin_session", wraps=server.create_admin_session) as create:
        assert call(server, "/admin/session", payload={"token": TOKENS["break_glass"]}) == (
            403,
            {"error": "break_glass_session_forbidden"},
            "",
        )
        create.assert_not_called()
    with server.kernel.database.read_transaction() as connection:
        audit = connection.execute(
            "SELECT actor,payload_json FROM audit_records "
            "WHERE action='admin_break_glass_rejected_session'"
        ).fetchall()
        assert len(audit) == 1
        assert audit[0]["actor"] == "web-break_glass"
        assert json.loads(audit[0]["payload_json"]) == {}


def test_real_private_runtime_artifact_requires_export_capability_for_download(
    server: NoyraHTTPServer,
) -> None:
    private = "M07-private-event-export-sentinel"
    EventStore(server.kernel.database).append(
        server.kernel.subject_id, "m07_test", "test", {"message": private}, privacy_level="private"
    )
    status, created, _cookie = call(
        server,
        "/api/v1/admin/export-jobs",
        payload={"kind": "runtime"},
        headers=authorization("export"),
    )
    assert status == 202, created
    job_id = created["job_id"]
    server.export_jobs.futures[job_id].result(timeout=60)
    job = server.export_jobs.get(job_id, subject_id=server.kernel.subject_id)
    assert job.status == "completed"
    artifact: bytes | None = None
    for role in [*TOKENS, "anonymous", "unknown"]:
        request = Request(
            f"http://127.0.0.1:{server.address[1]}/api/v1/admin/export-jobs/{job_id}/download",
            headers=authorization(role),
        )
        response: Any
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if role not in {"export", "admin", "break_glass"}:
                assert response.status == 401 and json.loads(body) == {"error": "unauthorized"}
                continue
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/zip"
            assert response.headers["X-Archive-SHA256"] == job.sha256
            assert hashlib.sha256(body).hexdigest() == job.sha256
            if artifact is None:
                artifact = body
            assert body == artifact
    assert artifact is not None
    with zipfile.ZipFile(BytesIO(artifact)) as archive:
        exported = b"\n".join(archive.read(name) for name in archive.namelist())
    assert private.encode() in exported
    assert all(token.encode() not in exported for token in TOKENS.values())
