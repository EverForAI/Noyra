from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .types import canonical_json, content_hash


def validate_wallet_timestamp(value: str) -> str:
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("wallet timestamp must include a timezone")
    return value


def wallet_journal_hash(row: Any) -> str:
    validate_wallet_timestamp(row["created_at"])
    return content_hash(
        {
            key: row[key]
            for key in (
                "journal_id",
                "subject_id",
                "order_id",
                "network_id",
                "asset_id",
                "journal_type",
                "amount",
                "created_at",
            )
        }
    )


def wallet_entry_hash(row: Any) -> str:
    validate_wallet_timestamp(row["created_at"])
    return content_hash(
        {
            key: row[key]
            for key in (
                "entry_id",
                "journal_id",
                "subject_id",
                "order_id",
                "account",
                "direction",
                "amount",
                "created_at",
            )
        }
    )


def legacy_journal_hash(row: Any) -> str:
    return content_hash(
        {
            "journal_id": row["journal_id"],
            "order_id": row["order_id"],
            "type": row["journal_type"],
            "amount": row["amount"],
        }
    )


def legacy_entry_hash(row: Any) -> str:
    return content_hash(
        {
            key: row[key]
            for key in (
                "entry_id",
                "journal_id",
                "account",
                "direction",
                "amount",
            )
        }
    )


def wallet_upgrade_fingerprint(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256(b"noyra-wallet-schema60-approval-v1\n")
    for table, primary_key in (
        ("schema_meta", "key"),
        ("subject_identity", "subject_id"),
        ("wallet_payment_policies", "subject_id"),
        ("wallet_payment_orders", "order_id"),
        ("wallet_ledger_journals", "journal_id"),
        ("wallet_ledger_entries", "entry_id"),
    ):
        digest.update((table + "\n").encode("ascii"))
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {primary_key}"):
            digest.update(canonical_json(dict(row)).encode("utf-8") + b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class WalletLegacyApproval:
    expected_fingerprint: str
    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_fingerprint):
            raise ValueError("wallet upgrade approval fingerprint is invalid")
        if not self.actor.strip() or self.actor.strip() in {"subject", "system"}:
            raise ValueError("wallet upgrade requires an identified operator")
        if len(self.actor) > 256 or not self.reason.strip() or len(self.reason) > 2000:
            raise ValueError("wallet upgrade approval actor or reason is invalid")
