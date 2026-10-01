from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from noyra.migration.wallet import WalletBindingReceipt, WalletMigration


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


def test_local_wallet_requires_explicit_approval_and_matching_address() -> None:
    plan = WalletMigration.plan(
        mode="local_wallet_transfer",
        source_address="0xabc",
        target_address="0xabc",
        signer_id=None,
        task_id="task-1",
        local_transfer_enabled=True,
    )
    with pytest.raises(ValueError, match="approval"):
        WalletMigration.apply_local_transfer(plan, approval=None)
    with pytest.raises(ValueError, match="address"):
        WalletMigration.apply_local_transfer(
            plan, approval={"task_id": "task-1", "address": "0xdef"}
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


def test_local_transfer_requires_one_time_approval_and_keeps_key_retained() -> None:
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
        )
