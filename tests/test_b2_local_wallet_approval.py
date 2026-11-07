from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from noyra.core import Database
from noyra.migration.local_wallet_approval import (
    assert_local_wallet_binding,
    authorize_material,
    grant_local_wallet,
    load_local_wallet,
    revoke_local_wallet,
)
from noyra.migration.runtime import MigrationRuntime
from noyra.migration.targets import TargetRegistry
from test_migration_manager import _manager

ADDRESS = "0x" + "11" * 20


def setup_local(tmp_path: Path) -> Any:
    database, manager = _manager(tmp_path)
    signing = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate()
    registry = TargetRegistry(database, manager.policy_store)
    registry.register(
        "Noyra-0001",
        target_id="target-local",
        public_key=base64.urlsafe_b64encode(signing.public_key().public_bytes_raw()).decode(),
        recipient_public_key=base64.urlsafe_b64encode(recipient.public_key().public_bytes_raw())
        .decode()
        .rstrip("="),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    policy = manager.policy_store.read_policy("Noyra-0001")
    policy = manager.policy_store.update_policy(
        "Noyra-0001",
        policy.revision,
        {"wallet_mode": "local_wallet_transfer", "local_wallet_transfer_enabled": True},
        "operator",
    )
    # Approval requires the freshly enrolled target to prove its identity.
    challenge = registry.issue_challenge("target-local", source_epoch="epoch-1")
    registry.attest(
        "target-local",
        challenge,
        base64.urlsafe_b64encode(signing.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    proposal = manager.create_proposal(
        subject_id="Noyra-0001",
        target_id="target-local",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="Synthetic local wallet",
        expires_at=(_fixed_now() + timedelta(hours=3)).isoformat(),
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="local-wallet")
    owner = SimpleNamespace(
        kernel=SimpleNamespace(database=database),
        wallet_signer=SimpleNamespace(address=ADDRESS),
        settings=SimpleNamespace(data_dir=tmp_path),
    )
    return SimpleNamespace(
        database=database,
        manager=manager,
        policy=policy,
        task=task,
        owner=owner,
        registry=registry,
        recipient=recipient,
    )


def _fixed_now() -> datetime:
    return datetime.now(UTC)


def test_private_config_cannot_substitute_for_a_source_wallet_approval(tmp_path: Path) -> None:
    case = setup_local(tmp_path)
    config = tmp_path / "secrets" / "migration-bindings"
    config.mkdir(parents=True, mode=0o700)
    path = config / "target-local.json"
    path.write_text(
        json.dumps(
            {
                "local_wallet_approval": {
                    "approval_id": "a" * 48,
                    "task_id": case.task.task_id,
                    "address": ADDRESS,
                    "channel_id": "channel-1",
                    "expires_at": (_fixed_now() + timedelta(hours=1)).isoformat(),
                }
            }
        )
    )
    path.chmod(0o600)
    with pytest.raises(ValueError, match="source local wallet approval"):
        MigrationRuntime.binding(
            cast(MigrationRuntime, SimpleNamespace(owner=case.owner)), case.task, case.policy
        )


def test_real_source_grant_is_idempotent_and_survives_database_reopen(tmp_path: Path) -> None:
    case = setup_local(tmp_path)
    grant = grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    assert grant_local_wallet(case.database, case.task, ADDRESS, actor="operator") == grant
    assert (
        load_local_wallet(Database(case.database.path, initialize=False), case.task, ADDRESS)
        == grant
    )
    binding = MigrationRuntime.binding(
        cast(MigrationRuntime, SimpleNamespace(owner=case.owner)), case.task, case.policy
    )
    assert binding["wallet_binding"]["approval"] == grant
    assert grant["channel_id"].startswith("recipient-")
    with case.database.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM migration_audit_events "
                "WHERE action='migration_local_wallet_approved'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "change", ["address", "policy", "target", "cancel", "expiry", "source_epoch"]
)
def test_wallet_authority_rejects_stale_scope(tmp_path: Path, change: str) -> None:
    case = setup_local(tmp_path)
    grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    address = ADDRESS
    task = case.task
    if change == "address":
        address = "0x" + "22" * 20
    elif change == "policy":
        case.manager.policy_store.update_policy(
            task.subject_id, case.policy.revision, {"min_free_bytes": 1}, "operator"
        )
    elif change == "target":
        # Re-enrollment keeps the same public material but changes the durable generation.
        with case.database.transaction() as c:
            row = c.execute(
                "SELECT * FROM migration_targets WHERE target_id='target-local'"
            ).fetchone()
            value = dict(row)
            value["enrollment_generation"] += 1
            c.execute(
                "UPDATE migration_targets SET enrollment_generation=?,state_hash=? "
                "WHERE target_id='target-local'",
                (
                    value["enrollment_generation"],
                    TargetRegistry._state_hash_from_row(value),
                ),
            )
    elif change == "cancel":
        case.manager.cancel(task.task_id, actor="operator", reason="Synthetic cancellation")
    elif change == "source_epoch":
        task = replace(task, source_epoch="runtime-other")
    else:
        with (
            patch(
                "noyra.migration.local_wallet_approval._now",
                return_value=_fixed_now() + timedelta(hours=2),
            ),
            pytest.raises(ValueError, match="expired"),
        ):
            load_local_wallet(case.database, task, address)
        return
    with pytest.raises(ValueError):
        load_local_wallet(case.database, task, address)


def test_revocation_never_falls_back_to_an_older_grant_and_regrant_is_explicit(
    tmp_path: Path,
) -> None:
    case = setup_local(tmp_path)
    grant = grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    with pytest.raises(ValueError, match="changed before revocation"):
        revoke_local_wallet(case.database, case.task, "wrong-id", actor="operator")
    revoke_local_wallet(case.database, case.task, grant["approval_id"], actor="operator")
    revoke_local_wallet(case.database, case.task, grant["approval_id"], actor="operator")
    with pytest.raises(ValueError, match="revoked"):
        load_local_wallet(case.database, case.task, ADDRESS)
    new = grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    assert new["approval_id"] != grant["approval_id"]
    with pytest.raises(ValueError, match="differs from source"):
        assert_local_wallet_binding(
            case.database,
            case.task,
            {"mode": "local_wallet_transfer", "address": ADDRESS, "approval": grant},
        )


def test_material_authorization_is_bound_to_one_artifact_with_retry_identity(
    tmp_path: Path,
) -> None:
    case = setup_local(tmp_path)
    grant = grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    binding = {"mode": "local_wallet_transfer", "address": ADDRESS, "approval": grant}
    authorize_material(
        case.database,
        case.task,
        binding,
        "artifact-one",
        recipient_key_fingerprint=grant["channel_id"].removeprefix("recipient-"),
    )
    authorize_material(
        case.database,
        case.task,
        binding,
        "artifact-one",
        recipient_key_fingerprint=grant["channel_id"].removeprefix("recipient-"),
    )
    with pytest.raises(ValueError, match="another artifact"):
        authorize_material(
            case.database,
            case.task,
            binding,
            "artifact-two",
            recipient_key_fingerprint=grant["channel_id"].removeprefix("recipient-"),
        )
    with case.database.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM migration_audit_events "
                "WHERE action='migration_local_wallet_material_authorized'"
            ).fetchone()[0]
            == 1
        )


def test_key_files_are_not_read_before_source_authority_is_established(tmp_path: Path) -> None:
    from noyra.migration.wallet_material import source_wallet_files

    case = setup_local(tmp_path)
    with (
        patch(
            "noyra.migration.wallet_material.load_account", side_effect=AssertionError("key read")
        ),
        pytest.raises(ValueError, match="approval is required"),
    ):
        source_wallet_files(
            case.task,
            {"mode": "local_wallet_transfer", "address": ADDRESS, "approval": {}},
            tmp_path / "rpc.json",
            database=case.database,
            artifact_id="artifact-one",
            recipient_key_fingerprint="0" * 64,
        )
    assert not (tmp_path / "rpc.json").exists()


def test_real_synthetic_keystore_is_exported_only_in_recipient_encrypted_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    from noyra.migration.bundle import decrypt_bundle
    from noyra.migration.http_executor import SQLiteArtifactProvider
    from noyra.migration.subject_payload import restore_payload
    from noyra.wallet.keystore import create_keystore, load_account

    case = setup_local(tmp_path)
    keystore = tmp_path / "wallet" / "test-wallet.json"
    password = tmp_path / "wallet" / "test-password"
    create_keystore(keystore, "synthetic-password", private_key="11" * 32)
    password.write_text("synthetic-password")
    password.chmod(0o600)
    account = load_account(keystore, "synthetic-password")
    grant = grant_local_wallet(case.database, case.task, account.address, actor="operator")
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(keystore))
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(password))
    monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", json.dumps({"1": "https://rpc.example.com"}))
    recipient_bytes = case.recipient.public_key().public_bytes_raw()
    proof = {
        "artifact_id": "synthetic-local-wallet",
        "recipient_public_key": base64.urlsafe_b64encode(recipient_bytes).decode().rstrip("="),
        "recipient_key_fingerprint": hashlib.sha256(recipient_bytes).hexdigest(),
        "target_id": case.task.target_id,
        "wallet_binding": {
            "mode": "local_wallet_transfer",
            "address": account.address,
            "approval": grant,
        },
    }
    provider = SQLiteArtifactProvider(case.database.path, tmp_path / "outgoing")
    bundle = provider(case.task, proof)
    decrypted = tmp_path / "payload.tar"
    decrypt_bundle(
        bundle.path,
        decrypted,
        recipient_private_key=case.recipient,
        manifest=bundle.manifest,
        expected_context={
            key: bundle.manifest[key]
            for key in (
                "task_id",
                "target_id",
                "source_epoch",
                "artifact_id",
                "subject_id",
                "schema_version",
                "artifact_format",
            )
        },
    )
    restore_payload(decrypted, tmp_path / "restored", case.task.subject_id)
    restored = load_account(
        tmp_path / "restored" / "migration-wallet" / "wallet.json", "synthetic-password"
    )
    assert restored.address == account.address
    with Database(
        tmp_path / "restored" / "noyra.sqlite3",
        initialize=False,
        read_only=True,
        immutable_snapshot=True,
    ).read_transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM migration_audit_events "
                "WHERE action='migration_local_wallet_material_authorized'"
            ).fetchone()[0]
            == 1
        )
    assert not list((tmp_path / "outgoing").glob("*.rpc.json"))
    assert not list((tmp_path / "outgoing").glob(".*.tar"))


def test_valid_wallet_grant_cannot_encrypt_keys_to_a_different_recipient(tmp_path: Path) -> None:
    case = setup_local(tmp_path)
    grant = grant_local_wallet(case.database, case.task, ADDRESS, actor="operator")
    binding = {"mode": "local_wallet_transfer", "address": ADDRESS, "approval": grant}
    with pytest.raises(ValueError, match="recipient differs"):
        authorize_material(
            case.database, case.task, binding, "artifact-one", recipient_key_fingerprint="0" * 64
        )
    with case.database.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM migration_audit_events "
                "WHERE action='migration_local_wallet_material_authorized'"
            ).fetchone()[0]
            == 0
        )


def test_operator_http_wallet_grant_retry_and_revoke_use_real_database_authority(
    tmp_path: Path,
) -> None:
    import urllib.error
    from urllib.request import Request, urlopen

    from pydantic import SecretStr

    from noyra.core import SubjectKernel
    from noyra.service import NoyraHTTPServer, ServiceSettings

    case = setup_local(tmp_path)
    kernel = SubjectKernel(case.database.path, case.task.subject_id, "f" * 64)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    # A real task normally captures this active runtime epoch at approval.
    # Keep a fresh task so the HTTP path exercises the real authority contract.
    policy = case.manager.policy_store.read_policy(case.task.subject_id)
    proposal = case.manager.create_proposal(
        subject_id=case.task.subject_id,
        target_id="target-local",
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="HTTP local approval",
        expires_at=(_fixed_now() + timedelta(hours=3)).isoformat(),
    )
    task = case.manager.approve(
        proposal.proposal_id, actor="operator", idempotency_key="http-wallet"
    )
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=case.task.subject_id,
        genesis_hash="f" * 64,
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("b2-wallet-admin-token-sufficient-entropy"),
    )
    server = NoyraHTTPServer(kernel, settings)
    server.wallet_signer = case.owner.wallet_signer
    server.start()
    url = f"http://127.0.0.1:{server.address[1]}/api/admin/migration/tasks/{task.task_id}/wallet-approval"

    def post(value: dict[str, Any], *, auth: bool = True) -> tuple[int, dict[str, Any]]:
        request = Request(
            url,
            data=json.dumps(value).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer b2-wallet-admin-token-sufficient-entropy" if auth else "",
            },
        )
        try:
            response = urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read())
            assert isinstance(payload, dict)
            return response.status, payload

    try:
        assert post({"authorize_local_wallet": True}, auth=False)[0] == 401
        code, first = post({"authorize_local_wallet": True})
        assert code == 200
        assert post({"authorize_local_wallet": True})[1] == first
        assert (
            load_local_wallet(case.database, task, ADDRESS)["approval_id"] == first["approval_id"]
        )
        assert post({"revoke_local_wallet": True, "approval_id": first["approval_id"]})[0] == 200
        assert post({"revoke_local_wallet": True, "approval_id": first["approval_id"]})[0] == 200
        with pytest.raises(ValueError, match="revoked"):
            load_local_wallet(case.database, task, ADDRESS)
    finally:
        server.close()
        kernel.close()
