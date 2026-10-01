"""Wallet migration plans that keep private keys outside migration artifacts."""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WalletMode = Literal["external_signer_rebind", "local_wallet_transfer", "disabled"]


@dataclass(frozen=True)
class WalletMigrationPlan:
    mode: WalletMode
    source_address: str
    target_address: str
    signer_id: str | None
    task_id: str
    private_key: None = None
    local_transfer_enabled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source_address": self.source_address,
            "target_address": self.target_address,
            "signer_id": self.signer_id,
            "task_id": self.task_id,
            "private_key": None,
        }


class WalletMigration:
    @staticmethod
    def plan(*, mode: WalletMode, source_address: str, target_address: str, signer_id: str | None, task_id: str, local_transfer_enabled: bool = False) -> WalletMigrationPlan:
        if mode == "disabled":
            return WalletMigrationPlan(mode, source_address, target_address, signer_id, task_id)
        if not source_address or source_address.casefold() != target_address.casefold():
            raise ValueError("wallet destination address must match source address")
        if mode == "external_signer_rebind" and not signer_id:
            raise ValueError("external signer identity is required")
        if mode == "local_wallet_transfer" and not local_transfer_enabled:
            raise ValueError("local wallet transfer is not explicitly enabled")
        return WalletMigrationPlan(mode, source_address, target_address, signer_id, task_id, None, local_transfer_enabled)

    @staticmethod
    def apply_external_signer(plan: WalletMigrationPlan) -> dict[str, Any]:
        if plan.mode != "external_signer_rebind" or not plan.signer_id:
            raise ValueError("plan is not an external signer rebind")
        return {"status": "rebind_required", **plan.public()}

    @staticmethod
    def apply_local_transfer(plan: WalletMigrationPlan, *, approval: dict[str, Any] | None) -> dict[str, Any]:
        if plan.mode != "local_wallet_transfer" or not plan.local_transfer_enabled:
            raise ValueError("local wallet transfer is not enabled")
        if not isinstance(approval, dict) or approval.get("task_id") != plan.task_id:
            raise ValueError("local wallet transfer requires a second approval")
        if str(approval.get("address", "")).casefold() != plan.source_address.casefold():
            raise ValueError("local wallet transfer approval address does not match")
        return {"status": "approved_once", **plan.public()}
