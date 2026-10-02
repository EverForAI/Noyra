"""Wallet migration plans that keep private keys outside migration artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from noyra.core.database import Database
from noyra.core.types import content_hash, new_id

WalletMode = Literal["external_signer_rebind", "local_wallet_transfer", "disabled"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


@dataclass(frozen=True)
class WalletMigrationPlan:
    mode: WalletMode
    source_address: str
    target_address: str
    signer_id: str | None
    task_id: str
    private_key: None = None
    local_transfer_enabled: bool = False
    plan_id: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "mode": self.mode,
            "source_address": self.source_address,
            "target_address": self.target_address,
            "signer_id": self.signer_id,
            "task_id": self.task_id,
            "private_key": None,
        }


@dataclass(frozen=True)
class WalletBindingReceipt:
    binding_id: str
    status: str
    mode: WalletMode
    task_id: str
    source_address: str
    target_address: str
    signer_id: str | None
    source_key_retained: bool = False
    committed: bool = False
    approval_fingerprint: str | None = None
    channel_id: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "status": self.status,
            "mode": self.mode,
            "task_id": self.task_id,
            "source_address": self.source_address,
            "target_address": self.target_address,
            "signer_id": self.signer_id,
            "source_key_retained": self.source_key_retained,
            "committed": self.committed,
            "approval_fingerprint": self.approval_fingerprint,
            "channel_id": self.channel_id,
        }


class WalletMigration:
    @staticmethod
    def plan(
        *,
        mode: WalletMode,
        source_address: str,
        target_address: str,
        signer_id: str | None,
        task_id: str,
        local_transfer_enabled: bool = False,
    ) -> WalletMigrationPlan:
        if mode not in {"external_signer_rebind", "local_wallet_transfer", "disabled"}:
            raise ValueError("wallet migration mode is invalid")
        if not isinstance(task_id, str) or _ID.fullmatch(task_id) is None:
            raise ValueError("wallet migration task id is invalid")
        if mode == "disabled":
            return WalletMigrationPlan(mode, source_address, target_address, signer_id, task_id)
        if (
            not isinstance(source_address, str)
            or not source_address
            or not isinstance(target_address, str)
            or source_address.casefold() != target_address.casefold()
        ):
            raise ValueError("wallet destination address must match source address")
        if mode == "external_signer_rebind" and (
            not isinstance(signer_id, str) or _ID.fullmatch(signer_id) is None
        ):
            raise ValueError("external signer identity is required")
        if mode == "local_wallet_transfer" and local_transfer_enabled is not True:
            raise ValueError("local wallet transfer is not explicitly enabled")
        return WalletMigrationPlan(
            mode,
            source_address,
            target_address,
            signer_id,
            task_id,
            None,
            local_transfer_enabled,
            new_id("wallet-migration"),
        )

    @staticmethod
    def apply_external_signer(
        plan: WalletMigrationPlan,
        *,
        target_signer_id: str | None = None,
        target_address: str | None = None,
    ) -> WalletBindingReceipt:
        if plan.mode != "external_signer_rebind" or not plan.signer_id:
            raise ValueError("plan is not an external signer rebind")
        if target_signer_id is not None and target_signer_id != plan.signer_id:
            raise ValueError("target signer identity does not match")
        if (
            target_address is not None
            and target_address.casefold() != plan.source_address.casefold()
        ):
            raise ValueError("target signer address does not match")
        return WalletBindingReceipt(
            binding_id=new_id("wallet-binding"),
            status="rebind_required",
            mode=plan.mode,
            task_id=plan.task_id,
            source_address=plan.source_address,
            target_address=plan.target_address,
            signer_id=plan.signer_id,
        )

    @staticmethod
    def apply_local_transfer(
        plan: WalletMigrationPlan,
        *,
        approval: dict[str, Any] | None,
        database: Database | None = None,
        subject_id: str | None = None,
    ) -> WalletBindingReceipt:
        if plan.mode != "local_wallet_transfer" or not plan.local_transfer_enabled:
            raise ValueError("local wallet transfer is not enabled")
        if not isinstance(approval, dict) or approval.get("task_id") != plan.task_id:
            raise ValueError("local wallet transfer requires a second approval")
        if str(approval.get("address", "")).casefold() != plan.source_address.casefold():
            raise ValueError("local wallet transfer approval address does not match")
        approval_id = approval.get("approval_id")
        channel_id = approval.get("channel_id")
        if not isinstance(approval_id, str) or _ID.fullmatch(approval_id) is None:
            raise ValueError("local wallet transfer requires a one-time approval id")
        if not isinstance(channel_id, str) or _ID.fullmatch(channel_id) is None:
            raise ValueError("local wallet transfer requires an encrypted channel")
        expires_at = approval.get("expires_at")
        if not isinstance(expires_at, str):
            raise ValueError("local wallet approval expiry is required")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("local wallet approval expiry is invalid") from error
        if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("local wallet approval has expired")
        if any(key in approval for key in ("private_key", "seed", "mnemonic", "password")):
            raise ValueError("local wallet approval must not contain key material")
        if database is None:
            raise ValueError("local wallet approval requires a durable database")
        fingerprint = content_hash(
            {"approval_id": approval_id, "task_id": plan.task_id, "address": plan.source_address}
        )
        with database.transaction() as connection:
            resolved_subject = subject_id
            if resolved_subject is None:
                task = connection.execute(
                    "SELECT subject_id FROM migration_tasks WHERE task_id=?", (plan.task_id,)
                ).fetchone()
                if task is None:
                    raise ValueError("migration task subject is required for wallet approval")
                resolved_subject = str(task["subject_id"])
            consumed = connection.execute(
                "SELECT payload_json FROM migration_audit_events "
                "WHERE subject_id=? AND action='wallet_local_approval_consumed'",
                (resolved_subject,),
            ).fetchall()
            for row in consumed:
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError) as error:
                    raise ValueError("wallet approval audit record is invalid") from error
                if (
                    isinstance(payload, dict)
                    and payload.get("task_id") == plan.task_id
                    and payload.get("approval_id") == approval_id
                ):
                    raise ValueError("local wallet approval was already consumed")
            now = datetime.now(UTC).isoformat()
            payload = {
                "task_id": plan.task_id,
                "approval_id": approval_id,
                "address": plan.source_address,
                "channel_id": channel_id,
                "expires_at": expires_at,
            }
            connection.execute(
                "INSERT INTO migration_audit_events("
                "audit_id, subject_id, action, actor, payload_json, occurred_at, state_hash) "
                "VALUES (?, ?, 'wallet_local_approval_consumed', 'operator', ?, ?, ?)",
                (
                    new_id("migration-audit"),
                    resolved_subject,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    now,
                    content_hash(payload),
                ),
            )
        return WalletBindingReceipt(
            binding_id=new_id("wallet-binding"),
            status="approved_once",
            mode=plan.mode,
            task_id=plan.task_id,
            source_address=plan.source_address,
            target_address=plan.target_address,
            signer_id=None,
            source_key_retained=True,
            approval_fingerprint=fingerprint,
            channel_id=channel_id,
        )

    @staticmethod
    def commit_local_transfer(receipt: WalletBindingReceipt) -> WalletBindingReceipt:
        if (
            not isinstance(receipt, WalletBindingReceipt)
            or receipt.mode != "local_wallet_transfer"
            or receipt.status != "approved_once"
            or not receipt.source_key_retained
        ):
            raise ValueError("local wallet transfer receipt is not commit-ready")
        return WalletBindingReceipt(
            **{
                **receipt.to_dict(),
                "status": "committed",
                "source_key_retained": False,
                "committed": True,
            }
        )
