from __future__ import annotations

# ruff: noqa: E501
import pytest

from noyra.migration.wallet import WalletMigration


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
        WalletMigration.apply_local_transfer(plan, approval={"task_id": "task-1", "address": "0xdef"})


def test_wallet_plan_rejects_address_mismatch() -> None:
    with pytest.raises(ValueError, match="address"):
        WalletMigration.plan(
            mode="external_signer_rebind", source_address="0xabc", target_address="0xdef",
            signer_id="kms", task_id="task-1",
        )
