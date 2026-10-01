from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings

ADMIN_TOKEN = "migration-test-admin-token-with-enough-entropy"


@pytest.fixture
def migration_http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        subject_id="Noyra-migration-test",
        genesis_hash=content_hash({"test": "migration-service"}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(ADMIN_TOKEN),
    )
    kernel = SubjectKernel(
        settings.data_dir / "noyra.sqlite3", settings.subject_id, settings.genesis_hash
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        yield server, f"http://127.0.0.1:{server.address[1]}"
    finally:
        server.close()
        kernel.close()


def _json_request(url: str, *, authenticated: bool) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"} if authenticated else {}
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _add_attested_target(server: NoyraHTTPServer) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    target = server.migration_targets.register(
        server.kernel.subject_id,
        target_id="migration-target-1",
        public_key=public,
        endpoint="https://target.example/migration",
        capabilities={},
        region="test",
        provider="test",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = server.migration_targets.issue_challenge(target.target_id, source_epoch="source-1")
    signature = base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode()
    server.migration_targets.attest(target.target_id, challenge, signature, actor="operator")


def test_migration_candidates_route_is_operator_only_and_discloses_missing_resource_evidence(
    migration_http: tuple[NoyraHTTPServer, str],
) -> None:
    server, base_url = migration_http
    policy = server.migration_store.read_policy(server.kernel.subject_id)
    server.migration_store.update_policy(
        server.kernel.subject_id, policy.revision, {"enabled": True}, "operator"
    )
    _add_attested_target(server)

    status, _ = _json_request(f"{base_url}/api/v1/admin/migration/candidates", authenticated=False)
    assert status == 401

    status, candidates = _json_request(
        f"{base_url}/api/v1/admin/migration/candidates", authenticated=True
    )
    assert status == 200
    assert candidates == [
        {
            "target_id": "migration-target-1",
            "status": "active",
            "trusted": True,
            "resources_verified": False,
            "eligible": False,
            "reasons": ["resource_observation_unavailable"],
        }
    ]


def test_disabled_migration_does_not_discover_candidates(
    migration_http: tuple[NoyraHTTPServer, str],
) -> None:
    _, base_url = migration_http
    status, candidates = _json_request(
        f"{base_url}/api/v1/admin/migration/candidates", authenticated=True
    )
    assert status == 200
    assert candidates == []


def test_migration_proposal_detail_is_subject_scoped_and_integrity_checked(
    migration_http: tuple[NoyraHTTPServer, str],
) -> None:
    server, base_url = migration_http
    policy = server.migration_store.read_policy(server.kernel.subject_id)
    policy = server.migration_store.update_policy(
        server.kernel.subject_id, policy.revision, {"enabled": True}, "operator"
    )
    _add_attested_target(server)
    proposal = server.migration_manager.create_proposal(
        subject_id=server.kernel.subject_id,
        target_id="migration-target-1",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="planned maintenance",
        expires_at="2099-01-01T00:00:00+00:00",
        evidence={"source_health": "sha256:evidence"},
    )

    status, details = _json_request(
        f"{base_url}/api/v1/admin/migration/proposals/{proposal.proposal_id}",
        authenticated=True,
    )
    assert status == 200
    assert isinstance(details, dict)
    assert details["proposal_id"] == proposal.proposal_id
    assert details["evidence"] == {"source_health": "sha256:evidence"}
    assert "state_hash" not in details

    with server.kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE migration_proposals SET reason='tampered' WHERE proposal_id=?",
            (proposal.proposal_id,),
        )
    status, error = _json_request(
        f"{base_url}/api/v1/admin/migration/proposals/{proposal.proposal_id}",
        authenticated=True,
    )
    assert status == 503
    assert error == {"error": "migration_proposal_integrity_unavailable"}
