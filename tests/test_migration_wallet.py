from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.migration.wallet import WalletBindingReceipt, WalletMigration


def _database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-wallet-migration"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    return database, subject_id


def test_external_signer_plan_contains_no_secret() -> None:
    plan = WalletMigration.plan(
        mode="external_signer_rebind",
        source_address="0xabc",
        target_address="0xabc",
        signer_id="kms-prod",
        task_id="task-1",
    )
    assert plan.private_key is None
    assert plan.signer_id == "kms-prod"


def test_local_wallet_requires_explicit_approval_and_matching_address(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    plan = WalletMigration.plan(
        mode="local_wallet_transfer",
        source_address="0xabc",
        target_address="0xabc",
        signer_id=None,
        task_id="task-1",
        local_transfer_enabled=True,
    )
    with pytest.raises(ValueError, match="approval"):
        WalletMigration.apply_local_transfer(
            plan, approval=None, database=database, subject_id=subject_id
        )
    with pytest.raises(ValueError, match="address"):
        WalletMigration.apply_local_transfer(
            plan,
            approval={"task_id": "task-1", "address": "0xdef"},
            database=database,
            subject_id=subject_id,
        )


def test_wallet_plan_rejects_address_mismatch() -> None:
    with pytest.raises(ValueError, match="address"):
        WalletMigration.plan(
            mode="external_signer_rebind",
            source_address="0xabc",
            target_address="0xdef",
            signer_id="kms",
            task_id="task-1",
        )


def test_external_signer_rebind_verifies_target_identity_and_address() -> None:
    plan = WalletMigration.plan(
        mode="external_signer_rebind",
        source_address="0xabc",
        target_address="0xabc",
        signer_id="kms-prod",
        task_id="task-1",
    )
    receipt = WalletMigration.apply_external_signer(
        plan, target_signer_id="kms-prod", target_address="0xabc"
    )
    assert isinstance(receipt, WalletBindingReceipt)
    assert receipt.status == "rebind_required"
    with pytest.raises(ValueError, match="identity"):
        WalletMigration.apply_external_signer(plan, target_signer_id="other")


def test_external_signer_verification_returns_target_bound_receipt() -> None:
    plan = WalletMigration.plan(
        mode="external_signer_rebind",
        source_address="0xabc",
        target_address="0xabc",
        signer_id="kms-prod",
        task_id="task-signer-proof",
    )
    from noyra.core.types import content_hash

    proof = content_hash(
        {
            "task_id": plan.task_id,
            "target_id": "target-1",
            "manifest_digest": "b" * 64,
            "signer_id": plan.signer_id,
            "address": plan.source_address,
            "target_identity": "target-host-1",
        }
    )
    receipt = WalletMigration.verify_external_signer(
        plan,
        {
            "status": "verified",
            "signer_id": "kms-prod",
            "address": "0xabc",
            "target_identity": "target-host-1",
            "proof_digest": proof,
        },
        target_id="target-1",
        manifest_digest="b" * 64,
    )
    assert receipt.status == "verified"
    assert receipt.proof_digest == proof


def test_local_transfer_requires_one_time_approval_and_keeps_key_retained(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    plan = WalletMigration.plan(
        mode="local_wallet_transfer",
        source_address="0xabc",
        target_address="0xabc",
        signer_id=None,
        task_id="task-2",
        local_transfer_enabled=True,
    )
    with pytest.raises(ValueError, match="one-time"):
        WalletMigration.apply_local_transfer(
            plan,
            approval={"task_id": "task-2", "address": "0xabc"},
            database=database,
            subject_id=subject_id,
        )
    receipt = WalletMigration.apply_local_transfer(
        plan,
        approval={
            "task_id": "task-2",
            "address": "0xabc",
            "approval_id": "approval-2",
            "channel_id": "channel-2",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
        database=database,
        subject_id=subject_id,
    )
    assert receipt.source_key_retained is True
    assert receipt.committed is False
    assert WalletMigration.commit_local_transfer(receipt).committed is True
    with pytest.raises(ValueError, match="already"):
        WalletMigration.apply_local_transfer(
            plan,
            approval={
                "task_id": "task-2",
                "address": "0xabc",
                "approval_id": "approval-2",
                "channel_id": "channel-2",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
            database=database,
            subject_id=subject_id,
        )


def test_local_wallet_approval_survives_process_state_reset(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    plan = WalletMigration.plan(
        mode="local_wallet_transfer",
        source_address="0xabc",
        target_address="0xabc",
        signer_id=None,
        task_id="task-restart",
        local_transfer_enabled=True,
    )
    approval = {
        "task_id": "task-restart",
        "address": "0xabc",
        "approval_id": "approval-restart",
        "channel_id": "channel-restart",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    WalletMigration.apply_local_transfer(
        plan, approval=approval, database=database, subject_id=subject_id
    )
    with pytest.raises(ValueError, match="already"):
        WalletMigration.apply_local_transfer(
            plan, approval=approval, database=database, subject_id=subject_id
        )
