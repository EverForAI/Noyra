from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.migration.manager import MigrationApprovalError, MigrationManager
from test_migration_manager import _manager


def test_strict_principal_review_does_not_count_a_legacy_binding(tmp_path: Path) -> None:
    database, legacy = _manager(tmp_path)
    policy = legacy.policy_store.read_policy("Noyra-0001")
    policy = legacy.policy_store.update_policy(
        "Noyra-0001", policy.revision, {"approval_mode": "dual_review"}, "operator"
    )
    proposal = legacy.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="Synthetic review",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    first = legacy.approve(
        proposal.proposal_id, actor="operator", idempotency_key="review", identity_binding="a" * 64
    )
    assert first.status == "awaiting_approval"
    strict = MigrationManager(database, legacy.policy_store, require_principal_identity=True)
    second = strict.approve(
        proposal.proposal_id, actor="operator", idempotency_key="review", reviewer_principal="bob"
    )
    assert second.status == "awaiting_approval"


def proposal_for(manager: MigrationManager) -> str:
    policy = manager.policy_store.read_policy("Noyra-0001")
    if policy.approval_mode != "dual_review":
        policy = manager.policy_store.update_policy(
            "Noyra-0001", policy.revision, {"approval_mode": "dual_review"}, "operator"
        )
    return manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="Synthetic review",
        expires_at="2099-01-01T00:00:00+00:00",
    ).proposal_id


def test_independent_principals_are_durable_idempotent_and_not_raw_identifiers(
    tmp_path: Path,
) -> None:
    database, legacy = _manager(tmp_path)
    strict = MigrationManager(database, legacy.policy_store, require_principal_identity=True)
    proposal = proposal_for(strict)
    pending = strict.approve(
        proposal, actor="operator", idempotency_key="review", reviewer_principal="alice"
    )
    assert pending.status == "awaiting_approval"
    reopened = MigrationManager(database, legacy.policy_store, require_principal_identity=True)
    duplicate = reopened.approve(
        proposal, actor="other-label", idempotency_key="review", reviewer_principal="alice"
    )
    assert duplicate.status == "awaiting_approval"
    approved = reopened.approve(
        proposal, actor="operator", idempotency_key="review", reviewer_principal="bob"
    )
    assert approved.status == "approved"
    view = reopened.get_proposal("Noyra-0001", proposal)
    assert view["review_semantics"] == "independent_principals"
    assert view["approval_count"] == 2
    with database.connection() as c:
        reopened.assert_review_authority(c, approved)
        rows = c.execute(
            "SELECT payload_json FROM migration_audit_events "
            "WHERE action='migration_review_identity_attested'"
        ).fetchall()
    assert len(rows) == 2
    assert not any("alice" in row[0] or "bob" in row[0] for row in rows)


def test_enabling_strict_review_cannot_execute_already_approved_legacy_task(tmp_path: Path) -> None:
    database, legacy = _manager(tmp_path)
    proposal = proposal_for(legacy)
    legacy.approve(proposal, actor="operator", idempotency_key="review", identity_binding="a" * 64)
    task = legacy.approve(
        proposal, actor="operator", idempotency_key="review", identity_binding="b" * 64
    )
    assert task.status == "approved"
    strict = MigrationManager(database, legacy.policy_store, require_principal_identity=True)
    with (
        database.connection() as c,
        pytest.raises(MigrationApprovalError, match="independent_review_incomplete"),
    ):
        strict.assert_review_authority(c, task)
    with pytest.raises(MigrationApprovalError, match="independent_review_incomplete"):
        strict.approve(
            proposal, actor="operator", idempotency_key="review", reviewer_principal="alice"
        )
    assert strict.get_proposal("Noyra-0001", proposal)["approval_count"] == 0
    assert legacy.get_proposal("Noyra-0001", proposal)["review_semantics"] == "legacy_bindings"


def test_single_operator_approval_does_not_require_independent_principals(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    policy = manager.policy_store.read_policy("Noyra-0001")
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="One operator",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert (
        manager.approve(proposal.proposal_id, actor="operator", idempotency_key="one").status
        == "approved"
    )
    assert (
        manager.get_proposal("Noyra-0001", proposal.proposal_id)["review_semantics"]
        == "self_attested"
    )


def test_cookie_sessions_and_bearer_share_the_issuing_credential_identity(tmp_path: Path) -> None:
    from noyra.core import SubjectKernel
    from noyra.core.types import content_hash
    from noyra.service import NoyraHTTPServer, ServiceSettings
    from test_migration_service import ADMIN_TOKEN, _add_attested_target

    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="Noyra-migration-test",
        genesis_hash=content_hash("b2-http"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(ADMIN_TOKEN),
        operator_token=SecretStr("b2-operator-token-with-sufficient-entropy"),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        _add_attested_target(server)
        policy = server.migration_store.read_policy(kernel.subject_id)
        policy = server.migration_store.update_policy(
            kernel.subject_id,
            policy.revision,
            {"enabled": True, "approval_mode": "dual_review"},
            "operator",
        )
        proposal = server.migration_manager.create_proposal(
            subject_id=kernel.subject_id,
            target_id="migration-target-1",
            policy_revision=policy.revision,
            reason_code="maintenance",
            reason="HTTP review",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        endpoint = f"http://127.0.0.1:{server.address[1]}/api/admin/migration/proposals/{proposal.proposal_id}/approve"

        def post(headers: dict[str, str]) -> dict[str, Any]:
            request = Request(
                endpoint, data=b"{}", headers={"Content-Type": "application/json", **headers}
            )
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == 202
                result = json.loads(response.read())
                assert isinstance(result, dict)
                return result

        assert post({"Authorization": "Bearer " + ADMIN_TOKEN})["approval_count"] == 1
        for _ in range(2):
            session_id, session = server.create_admin_session(role="admin", actor="web-admin")
            result = post(
                {"Cookie": "noyra_admin_session=" + session_id, "X-CSRF-Token": session.csrf_token}
            )
            assert result["approval_count"] == 1 and result["status"] == "awaiting_approval"
        assert (
            post({"Authorization": "Bearer b2-operator-token-with-sufficient-entropy"})["status"]
            == "approved"
        )
    finally:
        server.close()
        kernel.close()


def test_strict_setting_reaches_the_execution_coordinator(tmp_path: Path) -> None:
    from noyra.core import SubjectKernel
    from noyra.core.types import content_hash
    from noyra.service import NoyraHTTPServer, ServiceSettings

    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="b2-strict-executor",
        genesis_hash=content_hash("b2-strict"),
        migration_require_independent_review=True,
        migration_proxy_principal_hmac_key=SecretStr("b2-proxy-proof-key-sufficient-entropy"),
        trusted_proxy_cidrs=("127.0.0.1/32",),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    server = NoyraHTTPServer(kernel, settings)
    try:
        assert server.migration_manager.require_principal_identity
        assert server.migration_cutover.manager.require_principal_identity
    finally:
        server.close()
        kernel.close()


def test_independent_review_http_requires_two_request_authenticated_principals(
    tmp_path: Path,
) -> None:
    import hashlib
    import hmac
    import time

    from noyra.core import SubjectKernel
    from noyra.core.types import content_hash
    from noyra.service import NoyraHTTPServer, ServiceSettings
    from test_migration_service import ADMIN_TOKEN, _add_attested_target

    key = "b2-proxy-proof-key-sufficient-entropy"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="Noyra-migration-test",
        genesis_hash=content_hash("b2-http-principals"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(ADMIN_TOKEN),
        migration_require_independent_review=True,
        migration_proxy_principal_hmac_key=SecretStr(key),
        trusted_proxy_cidrs=("127.0.0.1/32",),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        _add_attested_target(server)
        policy = server.migration_store.read_policy(kernel.subject_id)
        policy = server.migration_store.update_policy(
            kernel.subject_id,
            policy.revision,
            {"enabled": True, "approval_mode": "dual_review"},
            "operator",
        )
        proposal = server.migration_manager.create_proposal(
            subject_id=kernel.subject_id,
            target_id="migration-target-1",
            policy_revision=policy.revision,
            reason_code="maintenance",
            reason="Authenticated principals",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        path = f"/api/admin/migration/proposals/{proposal.proposal_id}/approve"

        def post(
            principal: str | None, *, signed_as: str | None = None
        ) -> tuple[int, dict[str, Any]]:
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + ADMIN_TOKEN}
            if principal is not None:
                stamp = str(int(time.time()))
                payload = "\n".join(
                    (
                        "noyra-authenticated-principal/v1",
                        signed_as or principal,
                        stamp,
                        "POST",
                        path,
                    )
                ).encode()
                headers.update(
                    {
                        "X-Noyra-Authenticated-Principal": principal,
                        "X-Noyra-Principal-Timestamp": stamp,
                        "X-Noyra-Principal-Signature": hmac.new(
                            key.encode(), payload, hashlib.sha256
                        ).hexdigest(),
                    }
                )
            request = Request(
                f"http://127.0.0.1:{server.address[1]}" + path, data=b"{}", headers=headers
            )
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                value = json.loads(response.read())
                assert isinstance(value, dict)
                return response.status, value

        assert post(None)[0] == 409
        assert post("mallory", signed_as="alice")[0] == 409
        assert post("alice")[1]["approval_count"] == 1
        assert post("alice")[1]["status"] == "awaiting_approval"
        code, result = post("bob")
        assert code == 202 and result["approval_count"] == 2
        assert (
            result["review_semantics"] == "independent_principals"
            and result["status"] == "approved"
        )
    finally:
        server.close()
        kernel.close()


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({}, "NOYRA_MIGRATION_PROXY_PRINCIPAL_HMAC_KEY_FILE"),
        (
            {
                "migration_proxy_principal_hmac_key": SecretStr(
                    "synthetic-review-proof-key-32-bytes"
                )
            },
            "NOYRA_TRUSTED_PROXY_CIDRS",
        ),
        (
            {
                "migration_proxy_principal_hmac_key": SecretStr("synthetic-short"),
                "trusted_proxy_cidrs": ("127.0.0.1/32",),
            },
            "at least 32 bytes",
        ),
    ],
)
def test_independent_mode_rejects_incomplete_proxy_config_before_storage(
    tmp_path: Path, extra: dict[str, Any], expected: str
) -> None:
    from pydantic import ValidationError

    from noyra.core.types import content_hash
    from noyra.service import ServiceSettings

    data = tmp_path / "untouched"
    with pytest.raises(ValidationError) as error:
        ServiceSettings(
            data_dir=data,
            subject_id="review-configuration",
            genesis_hash=content_hash("review-configuration"),
            migration_require_independent_review=True,
            **extra,
        )
    assert expected in str(error.value)
    assert "synthetic-short" not in str(error.value)
    assert not data.exists()


def test_single_operator_configuration_keeps_independent_identity_optional(tmp_path: Path) -> None:
    from noyra.core.types import content_hash
    from noyra.service import ServiceSettings

    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="single-review-config",
        genesis_hash=content_hash("single-review-config"),
        migration_approval_mode="self_attested",
    )
    assert not settings.migration_require_independent_review
    assert settings.migration_proxy_principal_hmac_key is None
    assert not settings.trusted_proxy_cidrs


@pytest.mark.parametrize("strict", [False, True])
def test_policy_reports_server_dual_identity_mode_even_when_current_mode_is_disabled(
    tmp_path: Path, strict: bool
) -> None:
    from noyra.core import SubjectKernel
    from noyra.core.types import content_hash
    from noyra.service import NoyraHTTPServer, ServiceSettings
    from test_authentication_input_contract import call

    key = "synthetic-server-principal-config-key-32"
    token = "synthetic-policy-operator-token-32-bytes"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="review-mode-projection",
        genesis_hash=content_hash("review-mode-projection"),
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr(token),
        migration_require_independent_review=strict,
        migration_proxy_principal_hmac_key=SecretStr(key) if strict else None,
        trusted_proxy_cidrs=("127.0.0.1/32",) if strict else (),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        path = "/api/v1/admin/migration/policy"
        assert call(server, path)[0] == 401
        headers = {"Authorization": "Bearer " + token}
        status, result, _ = call(server, path, headers=headers)
        assert status == 200 and result["approval_mode"] == "disabled"
        assert result["review_semantics"] == "disabled"
        assert result["dual_review_semantics"] == (
            "independent_principals" if strict else "legacy_bindings"
        )
        assert key not in str(result) and token not in str(result)
        assert call(server, path.replace("/api/v1/", "/api/"), headers=headers)[1] == result
    finally:
        server.close()
        kernel.close()
