"""Bounded wallet projections with stable insertion anchors."""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
import time
from dataclasses import asdict
from typing import Any, ClassVar

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.identity import validate_subject_id
from noyra.core.redaction import redact_secret_text
from noyra.core.types import canonical_json, strict_json_loads, utc_now

from .acquisition import ACQUISITION_RUN_STATES, WalletBalanceAcquisitionLedger
from .economy import _WALLET_AUDIT_DETAIL_FIELDS, WalletEconomyStore, _parse_time
from .execution import WalletPaymentExecutionEngine
from .store import WalletStore


class WalletListLimit(RuntimeError):
    """A query exhausted its read budget; no truncated success is returned."""


class WalletAdminProjection:
    SOURCES: ClassVar[dict[str, tuple[str, str, str]]] = {
        "networks": ("wallet_networks", "network_id", "created_at"),
        "assets": ("wallet_assets", "asset_id", "created_at"),
        "addresses": ("wallet_addresses", "address_id", "created_at"),
        "balances": ("wallet_balance_snapshots", "snapshot_id", "observed_at"),
        "acquisitions": ("wallet_balance_acquisition_runs", "run_id", "created_at"),
        "bounties": ("wallet_bounties", "bounty_id", "created_at"),
        "orders": ("wallet_payment_orders", "order_id", "created_at"),
        "executions": ("wallet_payment_executions", "execution_id", "created_at"),
        "audits": ("audit_records", "audit_id", "occurred_at"),
    }
    STATUSES: ClassVar[dict[str, set[str]]] = {
        **{kind: {"active", "revoked"} for kind in ("networks", "assets", "addresses")},
        "acquisitions": set(ACQUISITION_RUN_STATES),
        "orders": {
            "pending_policy",
            "awaiting_confirmation",
            "reserved",
            "rejected",
            "cancelled",
            "expired",
            "signing",
            "broadcast",
            "unknown",
            "confirmed",
            "failed",
            "refunded",
        },
        "executions": {"signing", "broadcast", "unknown", "confirmed", "failed"},
    }

    def __init__(self, database: Database):
        self.database = database

    def page(
        self,
        subject_id: str,
        kind: str,
        *,
        limit: int = 25,
        status: str | None = None,
        network_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        validate_subject_id(subject_id)
        if kind not in self.SOURCES or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid wallet list query")
        if status is not None and status not in self.STATUSES.get(kind, set()):
            raise ValueError("invalid wallet status query")
        if network_id is not None and (
            kind in {"networks", "audits"}
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", network_id) is None
        ):
            raise ValueError("invalid wallet network query")
        table, key, timestamp = self.SOURCES[kind]
        decoded = None if cursor is None else self._decode(cursor)
        scope = [subject_id, kind, status, network_id]
        if decoded is not None and decoded["scope"] != scope:
            raise ValueError("wallet cursor scope mismatch")
        deadline = time.monotonic() + 3
        with self.database.read_transaction() as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            try:
                high_water = int(
                    connection.execute(
                        f'SELECT COALESCE(MAX(rowid),0) FROM "{table}" WHERE subject_id=?',
                        (subject_id,),
                    ).fetchone()[0]
                )
                anchor = high_water if decoded is None else decoded["anchor"]
                if anchor > high_water:
                    raise ValueError("wallet cursor outside current history")
                clauses = ["subject_id=?", "rowid<=?"]
                values: list[Any] = [subject_id, anchor]
                if kind == "audits":
                    clauses.append("substr(action,1,7)='wallet_'")
                if status is not None:
                    clauses.append("status=?")
                    values.append(status)
                if network_id is not None:
                    clauses.append("network_id=?")
                    values.append(network_id)
                if kind == "balances":
                    clauses.append(
                        "NOT EXISTS (SELECT 1 FROM wallet_balance_snapshots newer "
                        "WHERE newer.subject_id=wallet_balance_snapshots.subject_id "
                        "AND newer.asset_id=wallet_balance_snapshots.asset_id "
                        "AND newer.address_id=wallet_balance_snapshots.address_id "
                        "AND newer.rowid<=? "
                        "AND (newer.observed_at>wallet_balance_snapshots.observed_at OR "
                        "(newer.observed_at=wallet_balance_snapshots.observed_at AND "
                        "newer.snapshot_id>wallet_balance_snapshots.snapshot_id)))"
                    )
                    values.append(anchor)
                where = " AND ".join(clauses)
                total = int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE {where}', values
                    ).fetchone()[0]
                )
                if decoded is not None:
                    where += f' AND ("{timestamp}"<? OR ("{timestamp}"=? AND "{key}"<?))'
                    values.extend([decoded["time"], decoded["time"], decoded["key"]])
                rows = connection.execute(
                    f'SELECT * FROM "{table}" WHERE {where} '
                    f'ORDER BY "{timestamp}" DESC,"{key}" DESC LIMIT ?',
                    (*values, limit + 1),
                ).fetchall()
                items = [self._record(kind, row) for row in rows[:limit]]
                if time.monotonic() > deadline:
                    raise WalletListLimit("wallet list read budget exceeded")
            except sqlite3.OperationalError as error:
                if time.monotonic() > deadline:
                    raise WalletListLimit("wallet list read budget exceeded") from error
                raise
            finally:
                connection.set_progress_handler(None, 0)
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = (
                base64.urlsafe_b64encode(
                    canonical_json(
                        {
                            "v": 1,
                            "scope": scope,
                            "anchor": anchor,
                            "time": last[timestamp],
                            "key": last[key],
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {
            "items": items,
            "total": total,
            "has_more": len(rows) > limit,
            "next_cursor": next_cursor,
            "sampled_at": utc_now(),
            "coverage": "insertion_anchor_current_status",
        }

    @staticmethod
    def _decode(cursor: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError("invalid wallet cursor")
        try:
            data = strict_json_loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            raise ValueError("invalid wallet cursor") from error
        if (
            not isinstance(data, dict)
            or set(data) != {"v", "scope", "anchor", "time", "key"}
            or type(data["v"]) is not int
            or data["v"] != 1
            or type(data["anchor"]) is not int
            or not 0 <= data["anchor"] <= 2**63 - 1
            or not isinstance(data["scope"], list)
            or len(data["scope"]) != 4
            or not isinstance(data["time"], str)
            or not 1 <= len(data["time"]) <= 64
            or not isinstance(data["key"], str)
            or not 1 <= len(data["key"]) <= 128
        ):
            raise ValueError("invalid wallet cursor")
        return data

    @staticmethod
    def _record(kind: str, row: Any) -> dict[str, Any]:
        if kind == "networks":
            from urllib.parse import urlsplit

            result = asdict(WalletStore._network_from_row(row))
            # RPC paths can be credentials even without userinfo or query strings.
            result["rpc_host"] = urlsplit(result.pop("rpc_url") or "").hostname
            return result
        if kind == "assets":
            return asdict(WalletStore._asset_from_row(row))
        if kind == "addresses":
            return asdict(WalletStore._address_from_row(row))
        if kind == "balances":
            return asdict(WalletStore._balance_from_row(row))
        if kind == "acquisitions":
            WalletBalanceAcquisitionLedger._assert_run_hash(row)
            result = asdict(WalletBalanceAcquisitionLedger._run_from_row(row))
            result.pop("claim_token", None)
            result.pop("lease_owner", None)
            return result
        if kind == "bounties":
            if row["state_hash"] != WalletEconomyStore._bounty_state_hash_row(row, row["status"]):
                raise IntegrityError("wallet bounty state hash mismatch")
            status = (
                "expired"
                if row["status"] in {"draft", "published", "closed"}
                and _parse_time(row["expires_at"]) <= _parse_time(utc_now())
                else row["status"]
            )
            return asdict(WalletEconomyStore._bounty_from_row(row, status=status))
        if kind == "orders":
            if row["state_hash"] != WalletEconomyStore._order_state_hash_row(row, row["status"]):
                raise IntegrityError("wallet order state hash mismatch")
            return asdict(WalletEconomyStore._order_from_row(row))
        if kind == "executions":
            expected = WalletPaymentExecutionEngine._execution_hash_values_from_row(
                row,
                row["status"],
                row["tx_hash"],
                row["error_code"],
                row["receipt_status"],
                row["receipt_block_number"],
                row["receipt_block_hash"],
                row["receipt_confirmations"],
                row["receipt_effect_hash"],
                row["attempt_count"],
            )
            if row["state_hash"] != expected:
                raise IntegrityError("wallet execution state hash mismatch")
            return asdict(WalletPaymentExecutionEngine._execution_from_row(row))
        try:
            payload = strict_json_loads(row["payload_json"])
        except (ValueError, TypeError) as error:
            raise IntegrityError("wallet audit payload is invalid") from error
        if not isinstance(payload, dict):
            raise IntegrityError("wallet audit payload is invalid")
        details = {}
        for key in _WALLET_AUDIT_DETAIL_FIELDS:
            value = payload.get(key)
            if type(value) in {bool, int} or (isinstance(value, str) and len(value) <= 256):
                details[key] = redact_secret_text(value) if isinstance(value, str) else value
        return {
            "audit_id": row["audit_id"],
            "action": row["action"],
            "actor": redact_secret_text(str(row["actor"])),
            "occurred_at": row["occurred_at"],
            "details": details,
        }
