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
