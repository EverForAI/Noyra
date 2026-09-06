from __future__ import annotations

import base64
import binascii
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_json_loads,
    utc_now,
)

from .types import (
    SQLITE_INT64_MAX,
    WalletAddressInput,
    WalletAddressRecord,
    WalletAssetInput,
    WalletAssetRecord,
    WalletBalanceHistoryPage,
    WalletBalanceSnapshotInput,
    WalletBalanceSnapshotRecord,
    WalletNetworkInput,
    WalletNetworkRecord,
    canonical_timestamp,
    validate_timestamp,
)

_MAX_LIST_LIMIT = 1_000
WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE = _MAX_LIST_LIMIT
WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH = 8_192
WALLET_BALANCE_HISTORY_CURSOR_VERSION = "wallet-balance-history/v1"
# Observation health is deliberately conservative and bounded. A balance
# observation older than one day needs operator attention; callers may lower
# this window for tighter monitoring, but not extend it without bound.
WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS = 86_400
WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS = 2_592_000
# Keep diagnostics proportional to the configured registry while preventing a
# large asset/address cross-product from turning one read into an unbounded
# in-memory allocation.
WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS = 100_000
# Breakdown responses are intentionally bounded independently of the active
# pair cap. A caller can ask for fewer groups, but the store still refuses to
# construct an unbounded set of network/source buckets while it scans history.
WALLET_BALANCE_OBSERVATION_MAX_GROUPS = 256
WALLET_BALANCE_OBSERVATION_MAX_BREAKDOWN_GROUPS = WALLET_BALANCE_OBSERVATION_MAX_GROUPS
WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT = 100
_WALLET_OBSERVATION_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_URLSAFE_CURSOR_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_RESOURCE_META: dict[str, tuple[str, str, str]] = {
    "network": ("wallet_networks", "wallet_network_revisions", "network_id"),
    "asset": ("wallet_assets", "wallet_asset_revisions", "asset_id"),
    "address": ("wallet_addresses", "wallet_address_revisions", "address_id"),
}


@dataclass(frozen=True)
class _BalanceHistoryCursor:
    subject_id: str
    network_id: str | None
    asset_id: str | None
    address_id: str | None
    anchor: int
    observed_at: str
    snapshot_id: str


@dataclass(frozen=True)
class _WalletResourceState:
    """The small durable projection needed by relationship checks."""

    record: Any
    created_audit_id: str
    revoked_audit_id: str | None


class WalletStore:
    """Durable public wallet registrations and observed, read-only balances.

    This store deliberately owns only public metadata and observed integer
    balances. It does not receive, persist, derive, or expose signer material,
    and it has no transaction or payment method.
    """

    def __init__(self, database: Database):
        self.database = database

    def register_network(
        self,
        subject_id: str,
        proposal: WalletNetworkInput,
        *,
        actor: str,
    ) -> WalletNetworkRecord:
        self._require_operator(actor, "register a wallet network")
        validate_subject_id(subject_id)
        if not isinstance(proposal, WalletNetworkInput):
            raise TypeError("wallet network proposal is invalid")
        network_id = new_id("walletnet")
        now = utc_now()
        try:
            with self.database.transaction() as connection:
                conflict = connection.execute(
                    "SELECT network_id FROM wallet_networks WHERE subject_id = ? "
                    "AND (label = ? OR (chain_family = ? AND chain_id = ?)) LIMIT 1",
                    (subject_id, proposal.label, proposal.chain_family, proposal.chain_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("wallet network already registered")
                audit_id = self._append_audit(
                    connection,
                    subject_id,
                    "wallet_network_registered",
                    actor,
                    self._network_registration_audit_payload(network_id, proposal),
                )
                state_hash = self._network_hash(
                    network_id,
                    subject_id,
                    proposal.label,
                    proposal.chain_family,
                    proposal.chain_id,
                    proposal.native_symbol,
                    proposal.rpc_url,
                    "active",
                    now,
                    None,
                    None,
                    audit_id,
                    None,
                )
                connection.execute(
                    """INSERT INTO wallet_networks(
                        network_id, subject_id, label, chain_family, chain_id,
                        native_symbol, rpc_url, status, state_hash, created_at,
                        revoked_at, revoke_reason, created_audit_id, revoked_audit_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, NULL)""",
                    (
                        network_id,
                        subject_id,
                        proposal.label,
                        proposal.chain_family,
                        proposal.chain_id,
                        proposal.native_symbol,
                        proposal.rpc_url,
                        state_hash,
                        now,
                        audit_id,
                    ),
                )
                self._insert_revision(
                    connection,
                    "network",
                    network_id,
                    subject_id,
                    1,
                    "active",
                    "operator registered wallet network",
                    audit_id,
                    now,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("wallet network registration conflicts with durable state") from error
        return self.get_network(network_id, subject_id=subject_id)

    def register_asset(
        self,
        subject_id: str,
        proposal: WalletAssetInput,
        *,
        actor: str,
    ) -> WalletAssetRecord:
        self._require_operator(actor, "register a wallet asset")
        validate_subject_id(subject_id)
        if not isinstance(proposal, WalletAssetInput):
            raise TypeError("wallet asset proposal is invalid")
        asset_id = new_id("walletasset")
        now = utc_now()
        try:
            with self.database.transaction() as connection:
                network = self._network_row(connection, proposal.network_id, subject_id=subject_id)
                network_record = self._network_from_row(network)
                if network_record.status != "active":
                    raise ValueError("wallet network is not active")
                if (
                    proposal.asset_type == "native"
                    and proposal.symbol != network_record.native_symbol
                ):
                    raise ValueError("native asset symbol must match the network native symbol")
                if proposal.asset_type == "native":
                    conflict = connection.execute(
                        "SELECT asset_id FROM wallet_assets "
                        "WHERE subject_id = ? AND network_id = ? "
                        "AND asset_type = 'native' LIMIT 1",
                        (subject_id, proposal.network_id),
                    ).fetchone()
                else:
                    conflict = connection.execute(
                        "SELECT asset_id FROM wallet_assets "
                        "WHERE subject_id = ? AND network_id = ? "
                        "AND contract_address = ? LIMIT 1",
                        (subject_id, proposal.network_id, proposal.contract_address),
                    ).fetchone()
                if conflict is not None:
                    raise ValueError("wallet asset already registered")
                audit_id = self._append_audit(
                    connection,
                    subject_id,
                    "wallet_asset_registered",
                    actor,
                    self._asset_registration_audit_payload(asset_id, proposal),
                )
                state_hash = self._asset_hash(
                    asset_id,
                    subject_id,
                    proposal.network_id,
                    proposal.asset_type,
                    proposal.contract_address,
                    proposal.name,
                    proposal.symbol,
                    proposal.decimals,
                    "active",
                    now,
                    None,
                    None,
                    audit_id,
                    None,
                )
                connection.execute(
                    """INSERT INTO wallet_assets(
                        asset_id, subject_id, network_id, asset_type, contract_address,
                        name, symbol, decimals, status, state_hash, created_at,
                        revoked_at, revoke_reason, created_audit_id, revoked_audit_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, NULL)""",
                    (
                        asset_id,
                        subject_id,
                        proposal.network_id,
                        proposal.asset_type,
                        proposal.contract_address,
                        proposal.name,
                        proposal.symbol,
                        proposal.decimals,
                        state_hash,
                        now,
                        audit_id,
                    ),
                )
                self._insert_revision(
                    connection,
                    "asset",
                    asset_id,
                    subject_id,
                    1,
                    "active",
                    "operator registered wallet asset",
                    audit_id,
                    now,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("wallet asset registration conflicts with durable state") from error
        return self.get_asset(asset_id, subject_id=subject_id)

    def register_address(
        self,
        subject_id: str,
        proposal: WalletAddressInput,
        *,
        actor: str,
    ) -> WalletAddressRecord:
        self._require_operator(actor, "register a wallet address")
        validate_subject_id(subject_id)
        if not isinstance(proposal, WalletAddressInput):
            raise TypeError("wallet address proposal is invalid")
        address_id = new_id("walletaddr")
        now = utc_now()
        try:
            with self.database.transaction() as connection:
                network = self._network_row(connection, proposal.network_id, subject_id=subject_id)
                if self._network_from_row(network).status != "active":
                    raise ValueError("wallet network is not active")
                conflict = connection.execute(
                    "SELECT address_id FROM wallet_addresses "
                    "WHERE subject_id = ? AND network_id = ? "
                    "AND (label = ? OR address = ?) LIMIT 1",
                    (subject_id, proposal.network_id, proposal.label, proposal.address),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("wallet address already registered")
                audit_id = self._append_audit(
                    connection,
                    subject_id,
                    "wallet_address_registered",
                    actor,
                    self._address_registration_audit_payload(address_id, proposal),
                )
                state_hash = self._address_hash(
                    address_id,
                    subject_id,
                    proposal.network_id,
                    proposal.label,
                    proposal.address,
                    proposal.purpose,
                    "active",
                    now,
                    None,
                    None,
                    audit_id,
                    None,
                )
                connection.execute(
                    """INSERT INTO wallet_addresses(
                        address_id, subject_id, network_id, label, address, purpose,
                        status, state_hash, created_at, revoked_at, revoke_reason,
                        created_audit_id, revoked_audit_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, NULL)""",
                    (
                        address_id,
                        subject_id,
                        proposal.network_id,
                        proposal.label,
                        proposal.address,
                        proposal.purpose,
                        state_hash,
                        now,
                        audit_id,
                    ),
                )
                self._insert_revision(
                    connection,
                    "address",
                    address_id,
                    subject_id,
                    1,
                    "active",
                    "operator registered wallet address",
                    audit_id,
                    now,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("wallet address registration conflicts with durable state") from error
        return self.get_address(address_id, subject_id=subject_id)

    def revoke_network(
        self,
        network_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> WalletNetworkRecord:
        self._require_operator(actor, "revoke a wallet network")
        reason = self._reason(reason)
        validate_subject_id(subject_id)
        with self.database.transaction() as connection:
            row = self._network_row(connection, network_id, subject_id=subject_id)
            record = self._network_from_row(row)
            if record.status == "revoked":
                return record
            active_children = connection.execute(
                "SELECT 1 FROM wallet_assets WHERE subject_id = ? AND network_id = ? "
                "AND status = 'active' UNION ALL "
                "SELECT 1 FROM wallet_addresses WHERE subject_id = ? AND network_id = ? "
                "AND status = 'active' LIMIT 1",
                (subject_id, network_id, subject_id, network_id),
            ).fetchone()
            if active_children is not None:
                raise ValueError("wallet network has active asset or address registrations")
            now = utc_now()
            audit_id = self._append_audit(
                connection,
                subject_id,
                "wallet_network_revoked",
                actor,
                {"network_id": network_id, "reason": reason},
            )
            state_hash = self._network_hash(
                record.network_id,
                record.subject_id,
                record.label,
                record.chain_family,
                record.chain_id,
                record.native_symbol,
                record.rpc_url,
                "revoked",
                record.created_at,
                now,
                reason,
                row["created_audit_id"],
                audit_id,
            )
            connection.execute(
                """UPDATE wallet_networks SET status = 'revoked', state_hash = ?,
                    revoked_at = ?, revoke_reason = ?, revoked_audit_id = ?
                    WHERE network_id = ? AND subject_id = ?""",
                (state_hash, now, reason, audit_id, network_id, subject_id),
            )
            self._insert_revision(
                connection,
                "network",
                network_id,
                subject_id,
                2,
                "revoked",
                reason,
                audit_id,
                now,
            )
        return self.get_network(network_id, subject_id=subject_id)

    def revoke_asset(
        self,
        asset_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> WalletAssetRecord:
        self._require_operator(actor, "revoke a wallet asset")
        reason = self._reason(reason)
        validate_subject_id(subject_id)
        with self.database.transaction() as connection:
            row = self._asset_row(connection, asset_id, subject_id=subject_id)
            record = self._asset_from_row(row)
            if record.status == "revoked":
                return record
            now = utc_now()
            audit_id = self._append_audit(
                connection,
                subject_id,
                "wallet_asset_revoked",
                actor,
                {"asset_id": asset_id, "reason": reason},
            )
            state_hash = self._asset_hash(
                record.asset_id,
                record.subject_id,
                record.network_id,
                record.asset_type,
                record.contract_address,
                record.name,
                record.symbol,
                record.decimals,
                "revoked",
                record.created_at,
                now,
                reason,
                row["created_audit_id"],
                audit_id,
            )
            connection.execute(
                """UPDATE wallet_assets SET status = 'revoked', state_hash = ?,
                    revoked_at = ?, revoke_reason = ?, revoked_audit_id = ?
                    WHERE asset_id = ? AND subject_id = ?""",
                (state_hash, now, reason, audit_id, asset_id, subject_id),
            )
            self._insert_revision(
                connection,
                "asset",
                asset_id,
                subject_id,
                2,
                "revoked",
                reason,
                audit_id,
                now,
            )
        return self.get_asset(asset_id, subject_id=subject_id)

    def revoke_address(
        self,
        address_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> WalletAddressRecord:
        self._require_operator(actor, "revoke a wallet address")
        reason = self._reason(reason)
        validate_subject_id(subject_id)
        with self.database.transaction() as connection:
            row = self._address_row(connection, address_id, subject_id=subject_id)
            record = self._address_from_row(row)
            if record.status == "revoked":
                return record
            now = utc_now()
            audit_id = self._append_audit(
                connection,
                subject_id,
                "wallet_address_revoked",
                actor,
                {"address_id": address_id, "reason": reason},
            )
            state_hash = self._address_hash(
                record.address_id,
                record.subject_id,
                record.network_id,
                record.label,
                record.address,
                record.purpose,
                "revoked",
                record.created_at,
                now,
                reason,
                row["created_audit_id"],
                audit_id,
            )
            connection.execute(
                """UPDATE wallet_addresses SET status = 'revoked', state_hash = ?,
                    revoked_at = ?, revoke_reason = ?, revoked_audit_id = ?
                    WHERE address_id = ? AND subject_id = ?""",
                (state_hash, now, reason, audit_id, address_id, subject_id),
            )
            self._insert_revision(
                connection,
                "address",
                address_id,
                subject_id,
                2,
                "revoked",
                reason,
                audit_id,
                now,
            )
        return self.get_address(address_id, subject_id=subject_id)

    def record_balance_snapshot(
        self,
        subject_id: str,
        proposal: WalletBalanceSnapshotInput,
        *,
        actor: str,
    ) -> WalletBalanceSnapshotRecord:
        """Persist an operator-controlled observation without contacting a network."""

        self._require_operator(actor, "record a wallet balance observation")
        validate_subject_id(subject_id)
        if not isinstance(proposal, WalletBalanceSnapshotInput):
            raise TypeError("wallet balance proposal is invalid")
        snapshot_id = new_id("walletbal")
        created_at = utc_now()
        try:
            with self.database.transaction() as connection:
                return self._record_balance_snapshot_connection(
                    connection,
                    subject_id,
                    proposal,
                    actor=actor,
                    snapshot_id=snapshot_id,
                    created_at=created_at,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("wallet balance snapshot conflicts with durable state") from error

    def _record_balance_snapshot_connection(
        self,
        connection: Any,
        subject_id: str,
        proposal: WalletBalanceSnapshotInput,
        *,
        actor: str,
        snapshot_id: str | None = None,
        created_at: str | None = None,
    ) -> WalletBalanceSnapshotRecord:
        """Insert one observation on a caller-owned transaction.

        The acquisition ledger uses this narrow internal seam to commit the
        immutable snapshot and its run transition atomically.  It deliberately
        accepts no network URL or RPC payload; the target still comes from the
        subject-owned registrations.
        """

        self._require_operator(actor, "record a wallet balance observation")
        validate_subject_id(subject_id)
        if not isinstance(proposal, WalletBalanceSnapshotInput):
            raise TypeError("wallet balance proposal is invalid")
        resolved_snapshot_id = snapshot_id or new_id("walletbal")
        self._identifier(resolved_snapshot_id, "balance snapshot identifier")
        resolved_created_at = created_at or utc_now()
        try:
            if canonical_timestamp(resolved_created_at) != resolved_created_at:
                raise ValueError("wallet snapshot creation time is not canonical")
        except (TypeError, ValueError) as error:
            raise ValueError("wallet snapshot creation time is invalid") from error
        observed_at = proposal.observed_at or resolved_created_at
        asset = self._asset_row(connection, proposal.asset_id, subject_id=subject_id)
        address = self._address_row(connection, proposal.address_id, subject_id=subject_id)
        asset_record = self._asset_from_row(asset)
        address_record = self._address_from_row(address)
        if (
            asset_record.status != "active"
            or address_record.status != "active"
            or asset_record.network_id != address_record.network_id
        ):
            raise ValueError("wallet balance references are not active on one network")
        network = self._network_row(connection, asset_record.network_id, subject_id=subject_id)
        if self._network_from_row(network).status != "active":
            raise ValueError("wallet network is not active")
        state_hash = self._balance_hash(
            resolved_snapshot_id,
            subject_id,
            asset_record.network_id,
            proposal.asset_id,
            proposal.address_id,
            proposal.balance,
            proposal.source,
            observed_at,
            resolved_created_at,
        )
        connection.execute(
            """INSERT INTO wallet_balance_snapshots(
                snapshot_id, subject_id, network_id, asset_id, address_id,
                balance, source, observed_at, created_at, state_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                resolved_snapshot_id,
                subject_id,
                asset_record.network_id,
                proposal.asset_id,
                proposal.address_id,
                proposal.balance,
                proposal.source,
                observed_at,
                resolved_created_at,
                state_hash,
            ),
        )
        row = connection.execute(
            "SELECT * FROM wallet_balance_snapshots WHERE snapshot_id = ? AND subject_id = ?",
            (resolved_snapshot_id, subject_id),
        ).fetchone()
        if row is None:
            raise IntegrityError("wallet balance snapshot disappeared after insertion")
        return self._balance_from_row(row)

    def get_network(self, network_id: str, *, subject_id: str) -> WalletNetworkRecord:
        with self.database.connection() as connection:
            return self._network_from_row(
                self._network_row(connection, network_id, subject_id=subject_id)
            )

    def get_asset(self, asset_id: str, *, subject_id: str) -> WalletAssetRecord:
        with self.database.connection() as connection:
            return self._asset_from_row(
                self._asset_row(connection, asset_id, subject_id=subject_id)
            )

    def get_address(self, address_id: str, *, subject_id: str) -> WalletAddressRecord:
        with self.database.connection() as connection:
            return self._address_from_row(
                self._address_row(connection, address_id, subject_id=subject_id)
            )

    def get_balance_snapshot(
        self, snapshot_id: str, *, subject_id: str
    ) -> WalletBalanceSnapshotRecord:
        self._identifier(snapshot_id, "balance snapshot identifier")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM wallet_balance_snapshots WHERE snapshot_id = ? AND subject_id = ?",
                (snapshot_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"wallet balance snapshot not found: {snapshot_id}")
            return self._balance_from_row(row)

    def list_networks(
        self, subject_id: str, *, include_revoked: bool = True, limit: int = _MAX_LIST_LIMIT
    ) -> list[WalletNetworkRecord]:
        validate_subject_id(subject_id)
        clauses = ["subject_id = ?"]
        if not include_revoked:
            clauses.append("status = 'active'")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_networks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, network_id DESC LIMIT ?",
                (subject_id, self._limit(limit)),
            ).fetchall()
        return [self._network_from_row(row) for row in rows]

    def list_assets(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        include_revoked: bool = True,
        limit: int = _MAX_LIST_LIMIT,
    ) -> list[WalletAssetRecord]:
        validate_subject_id(subject_id)
        clauses = ["subject_id = ?"]
        values: list[object] = [subject_id]
        if network_id is not None:
            self._identifier(network_id, "network identifier")
            clauses.append("network_id = ?")
            values.append(network_id)
        if not include_revoked:
            clauses.append("status = 'active'")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_assets WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, asset_id DESC LIMIT ?",
                (*values, self._limit(limit)),
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def list_addresses(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        include_revoked: bool = True,
        limit: int = _MAX_LIST_LIMIT,
    ) -> list[WalletAddressRecord]:
        validate_subject_id(subject_id)
        clauses = ["subject_id = ?"]
        values: list[object] = [subject_id]
        if network_id is not None:
            self._identifier(network_id, "network identifier")
            clauses.append("network_id = ?")
            values.append(network_id)
        if not include_revoked:
            clauses.append("status = 'active'")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_addresses WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, address_id DESC LIMIT ?",
                (*values, self._limit(limit)),
            ).fetchall()
        return [self._address_from_row(row) for row in rows]

    def list_balance_snapshots(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        asset_id: str | None = None,
        address_id: str | None = None,
        limit: int = _MAX_LIST_LIMIT,
    ) -> list[WalletBalanceSnapshotRecord]:
        where, values = self._balance_filters(subject_id, network_id, asset_id, address_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_balance_snapshots WHERE "
                + where
                + " ORDER BY observed_at DESC, snapshot_id DESC LIMIT ?",
                (*values, self._limit(limit)),
            ).fetchall()
        return [self._balance_from_row(row) for row in rows]

    def balance_history_page(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        asset_id: str | None = None,
        address_id: str | None = None,
        limit: int = WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE,
        cursor: str | None = None,
    ) -> WalletBalanceHistoryPage:
        """Read one immutable balance-history page with a stable keyset cursor.

        The first page captures the largest SQLite rowid as a high-water mark.
        Subsequent pages retain that mark in the opaque cursor, so observations
        inserted while a caller is walking history cannot appear halfway through
        the traversal, even when their business timestamp is backdated.
        """

        bounded = self._history_limit(limit)
        where, values = self._balance_filters(subject_id, network_id, asset_id, address_id)
        requested_filters = (network_id, asset_id, address_id)
        decoded = None if cursor is None else self._decode_balance_history_cursor(cursor)
        if decoded is not None and (
            decoded.subject_id != subject_id
            or (
                decoded.network_id,
                decoded.asset_id,
                decoded.address_id,
            )
            != requested_filters
        ):
            raise ValueError("wallet balance history cursor does not match the query")

        with self.database.read_transaction() as connection:
            high_water_row = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) AS high_water FROM wallet_balance_snapshots"
            ).fetchone()
            if high_water_row is None:
                raise IntegrityError("wallet balance history high-water mark is unavailable")
            high_water = high_water_row["high_water"]
            if (
                isinstance(high_water, bool)
                or not isinstance(high_water, int)
                or not 0 <= high_water <= SQLITE_INT64_MAX
            ):
                raise IntegrityError("wallet balance history high-water mark is invalid")
            anchor = high_water if decoded is None else decoded.anchor
            if anchor > high_water:
                raise ValueError("wallet balance history cursor is outside the current history")

            cursor_clause = ""
            cursor_values: list[object] = []
            if decoded is not None:
                cursor_clause = " AND (observed_at < ? OR (observed_at = ? AND snapshot_id < ?))"
                cursor_values.extend(
                    (decoded.observed_at, decoded.observed_at, decoded.snapshot_id)
                )
            rows = connection.execute(
                "SELECT * FROM wallet_balance_snapshots WHERE "
                + where
                + " AND rowid <= ?"
                + cursor_clause
                + " ORDER BY observed_at DESC, snapshot_id DESC LIMIT ?",
                (*values, anchor, *cursor_values, bounded + 1),
            ).fetchall()

        records = [self._balance_from_row(row) for row in rows]
        has_more = len(records) > bounded
        items = tuple(records[:bounded])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = self._encode_balance_history_cursor(
                subject_id,
                network_id,
                asset_id,
                address_id,
                anchor,
                last.observed_at,
                last.snapshot_id,
            )
        return WalletBalanceHistoryPage(items, next_cursor, has_more)

    def latest_balances(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        asset_id: str | None = None,
        address_id: str | None = None,
        limit: int = _MAX_LIST_LIMIT,
    ) -> list[WalletBalanceSnapshotRecord]:
        where, values = self._balance_filters(subject_id, network_id, asset_id, address_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT s.* FROM wallet_balance_snapshots s WHERE "
                + where.replace("wallet_balance_snapshots.", "s.")
                + " AND NOT EXISTS ("
                "SELECT 1 FROM wallet_balance_snapshots newer "
                "WHERE newer.subject_id = s.subject_id AND newer.network_id = s.network_id "
                "AND newer.asset_id = s.asset_id AND newer.address_id = s.address_id "
                "AND (newer.observed_at > s.observed_at "
                "OR (newer.observed_at = s.observed_at AND newer.snapshot_id > s.snapshot_id))"
                ") ORDER BY s.observed_at DESC, s.snapshot_id DESC LIMIT ?",
                (*values, self._limit(limit)),
            ).fetchall()
        return [self._balance_from_row(row) for row in rows]

    def observation_health(
        self,
        subject_id: str,
        *,
        max_age_seconds: int = WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
        now: datetime | str | None = None,
    ) -> dict[str, int | str | None]:
        """Return a bounded health projection for active balance pairs.

        The projection is intentionally aggregate-only.  An active pair is an
        active asset/address/network combination belonging to ``subject_id``.
        Its newest immutable snapshot is classified against one canonical UTC
        evaluation time.  ``near_expiry_pairs`` covers the final quarter of the
        configured window (including the exact boundary); ``stale_pairs`` is
        strictly older than that window.  Pairs without a snapshot are counted
        as ``never_pairs`` and are not silently folded into stale data.

        ``anomalous_pairs`` is the number of active pairs for which adjacent
        snapshots, ordered newest first by ``observed_at`` and
        ``snapshot_id``, either disagree at the same timestamp or change by
        more than 100% (a zero/non-zero transition is always anomalous).  The
        comparison is performed with Python arbitrary-precision integers so
        the 256-digit storage bound cannot overflow or lose precision.
        """

        validate_subject_id(subject_id)
        age_limit = self._observation_age_limit(max_age_seconds)
        reference = self._observation_reference(now)
        with self.database.read_transaction() as connection:
            return self._observation_health_connection(
                connection,
                subject_id,
                age_limit=age_limit,
                reference=reference,
            )

    def observation_health_breakdown(
        self,
        subject_id: str,
        *,
        group_by: str = "network",
        network_id: str | None = None,
        source: str | None = None,
        limit: int = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
        max_age_seconds: int = WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded health breakdown grouped by network or source.

        This is an aggregate-only, read-only projection.  A source bucket is
        selected from each pair's newest active snapshot, while all active
        history for that pair contributes to ``snapshot_count`` and anomaly
        detection.  Pairs without history are represented by a ``null`` source
        bucket (unless a source filter is explicitly supplied).
        """

        validate_subject_id(subject_id)
        grouping = self._observation_group_by(group_by)
        network_filter = self._observation_optional_identifier(network_id, "network identifier")
        source_filter = self._observation_source(source)
        bounded_limit = self._observation_group_limit(limit)
        age_limit = self._observation_age_limit(max_age_seconds)
        reference = self._observation_reference(now)
        with self.database.read_transaction() as connection:
            if network_filter is not None:
                row = connection.execute(
                    "SELECT * FROM wallet_networks WHERE network_id = ? AND subject_id = ?",
                    (network_filter, subject_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"wallet network not found: {network_filter}")
                self._network_from_row(row)
            return self._observation_health_breakdown_connection(
                connection,
                subject_id,
                grouping=grouping,
                network_id=network_filter,
                source=source_filter,
                limit=bounded_limit,
                age_limit=age_limit,
                reference=reference,
            )

    def observation_health_by_network(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        source: str | None = None,
        limit: int = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
        max_age_seconds: int = WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper for a network-grouped health breakdown."""

        return self.observation_health_breakdown(
            subject_id,
            group_by="network",
            network_id=network_id,
            source=source,
            limit=limit,
            max_age_seconds=max_age_seconds,
            now=now,
        )

    def observation_health_by_source(
        self,
        subject_id: str,
        *,
        network_id: str | None = None,
        source: str | None = None,
        limit: int = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
        max_age_seconds: int = WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper for a newest-source-grouped breakdown."""

        return self.observation_health_breakdown(
            subject_id,
            group_by="source",
            network_id=network_id,
            source=source,
            limit=limit,
            max_age_seconds=max_age_seconds,
            now=now,
        )

    def _observation_health_breakdown_connection(
        self,
        connection: Any,
        subject_id: str,
        *,
        grouping: str,
        network_id: str | None,
        source: str | None,
        limit: int,
        age_limit: int,
        reference: datetime,
    ) -> dict[str, Any]:
        """Compute one breakdown against a caller-owned read snapshot."""

        stale_window = timedelta(seconds=age_limit)
        near_cutoff = (
            None if age_limit == 0 else timedelta(microseconds=(age_limit * 3 * 1_000_000) // 4)
        )

        # The registry is bounded before the history cursor is opened.  This
        # map is at most WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS entries and
        # is used only to distinguish current active pairs from retired history.
        active_pairs: dict[tuple[str, str, str], str] = {}
        active_query = (
            "SELECT a.network_id, a.asset_id, d.address_id "
            "FROM wallet_assets a "
            "JOIN wallet_addresses d ON d.subject_id = a.subject_id "
            "AND d.network_id = a.network_id AND d.status = 'active' "
            "JOIN wallet_networks n ON n.subject_id = a.subject_id "
            "AND n.network_id = a.network_id AND n.status = 'active' "
            "WHERE a.subject_id = ? AND a.status = 'active'"
        )
        active_parameters: tuple[object, ...]
        if network_id is not None:
            active_query += " AND a.network_id = ?"
            active_parameters = (
                subject_id,
                network_id,
                WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS + 1,
            )
        else:
            active_parameters = (subject_id, WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS + 1)
        # The result is reduced into a key map and rendered in sorted group
        # order below, so the registry probe needs no SQL sort (and therefore
        # cannot spill a large join into a temporary B-tree).
        active_query += " LIMIT ?"
        for row in connection.execute(active_query, active_parameters):
            try:
                key = (
                    self._identifier(row["network_id"], "network identifier"),
                    self._identifier(row["asset_id"], "asset identifier"),
                    self._identifier(row["address_id"], "address identifier"),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise IntegrityError("wallet observation target identity is invalid") from error
            if key in active_pairs:
                raise IntegrityError("wallet observation target is duplicated")
            active_pairs[key] = key[0]
            if len(active_pairs) > WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS:
                raise IntegrityError("wallet observation active pair limit is exceeded")

        groups: dict[str | None, dict[str, Any]] = {}

        def get_group(value: str | None) -> dict[str, Any]:
            if value not in groups:
                if len(groups) >= WALLET_BALANCE_OBSERVATION_MAX_GROUPS:
                    raise IntegrityError("wallet observation breakdown group limit is exceeded")
                groups[value] = {
                    "value": value,
                    "active_pairs": 0,
                    "observed_pairs": 0,
                    "fresh_pairs": 0,
                    "near_expiry_pairs": 0,
                    "stale_pairs": 0,
                    "never_pairs": 0,
                    "future_pairs": 0,
                    "anomalous_pairs": 0,
                    "snapshot_count": 0,
                    "latest_observed_at": None,
                    "max_age_seconds": None,
                }
            return groups[value]

        # ``seen_pairs`` contains only pairs with at least one valid active
        # snapshot.  The bounded registry pass below accounts for never pairs.
        seen_pairs: set[tuple[str, str, str]] = set()
        current_key: tuple[str, str, str] | None = None
        current_group: dict[str, Any] | None = None
        current_include = False
        current_seen = False
        current_anomalous = False
        previous_balance: int | None = None
        previous_balance_text: str | None = None
        previous_observed_at: str | None = None

        def finish_pair() -> None:
            nonlocal current_key, current_group, current_include, current_seen
            nonlocal current_anomalous, previous_balance, previous_balance_text
            nonlocal previous_observed_at
            if (
                current_key is not None
                and current_include
                and current_group is not None
                and current_anomalous
            ):
                current_group["anomalous_pairs"] += 1
            current_key = None
            current_group = None
            current_include = False
            current_seen = False
            current_anomalous = False
            previous_balance = None
            previous_balance_text = None
            previous_observed_at = None

        history_query = (
            "SELECT s.*, n.subject_id AS network_subject_id, n.status AS network_status, "
            "a.subject_id AS asset_subject_id, a.network_id AS asset_network_id, "
            "a.status AS asset_status, d.subject_id AS address_subject_id, "
            "d.network_id AS address_network_id, d.status AS address_status "
            "FROM wallet_balance_snapshots s "
            "LEFT JOIN wallet_networks n ON n.network_id = s.network_id "
            "LEFT JOIN wallet_assets a ON a.asset_id = s.asset_id "
            "LEFT JOIN wallet_addresses d ON d.address_id = s.address_id "
            "WHERE s.subject_id = ?"
        )
        history_parameters: tuple[object, ...] = (subject_id,)
        if network_id is not None:
            history_query += " AND s.network_id = ?"
            history_parameters = (subject_id, network_id)
        history_query += (
            " ORDER BY s.network_id, s.asset_id, s.address_id, "
            "s.observed_at DESC, s.snapshot_id DESC"
        )

        for row in connection.execute(history_query, history_parameters):
            record = self._balance_from_row(row)
            key = (record.network_id, record.asset_id, record.address_id)
            if key != current_key:
                finish_pair()
                current_key = key

            # Validate every row's relationship, including immutable history
            # belonging to revoked resources, before deciding whether it is
            # eligible for the current active projection.
            if not self._observation_snapshot_is_active(row, record, subject_id):
                continue
            if key not in active_pairs:
                raise IntegrityError("wallet observation pair count is inconsistent")

            if not current_seen:
                current_seen = True
                seen_pairs.add(key)
                # The cursor is newest-first, so this row determines the pair's
                # latest source and health class.
                selected = source is None or record.source == source
                if grouping == "network":
                    group_value: str | None = key[0]
                else:
                    group_value = record.source
                current_include = selected
                if current_include:
                    current_group = get_group(group_value)
                    current_group["active_pairs"] += 1
                    current_group["observed_pairs"] += 1
                    current_group["snapshot_count"] += 1
                    latest_observed_at = current_group["latest_observed_at"]
                    if latest_observed_at is None or record.observed_at > latest_observed_at:
                        current_group["latest_observed_at"] = record.observed_at
                    parsed = self._persisted_observation_datetime(record.observed_at)
                    delta = reference - parsed
                    if delta < timedelta(0):
                        current_group["future_pairs"] += 1
                    else:
                        age_seconds = delta.days * 86_400 + delta.seconds
                        old_max = current_group["max_age_seconds"]
                        current_group["max_age_seconds"] = (
                            age_seconds if old_max is None else max(old_max, age_seconds)
                        )
                        if delta > stale_window:
                            current_group["stale_pairs"] += 1
                        elif near_cutoff is not None and delta >= near_cutoff:
                            current_group["near_expiry_pairs"] += 1
                        else:
                            current_group["fresh_pairs"] += 1
                previous_balance = int(record.balance)
                previous_balance_text = record.balance
                previous_observed_at = record.observed_at
                continue

            current_balance = int(record.balance)
            if (
                previous_balance_text is not None
                and previous_observed_at is not None
                and record.observed_at == previous_observed_at
                and record.balance != previous_balance_text
            ):
                current_anomalous = True
            elif previous_balance is not None:
                low = min(previous_balance, current_balance)
                high = max(previous_balance, current_balance)
                if (low == 0 and high != 0) or (low != 0 and high > low * 2):
                    current_anomalous = True
            if current_include and current_group is not None:
                current_group["snapshot_count"] += 1
            previous_balance = current_balance
            previous_balance_text = record.balance
            previous_observed_at = record.observed_at
        finish_pair()

        # Account for active pairs with no valid active snapshot.  A source
        # filter intentionally excludes such pairs because they have no latest
        # source to match; without that filter they are grouped under null for
        # source views and under their network for network views.
        for key, pair_network in active_pairs.items():
            if key in seen_pairs or source is not None:
                continue
            group_value = pair_network if grouping == "network" else None
            group = get_group(group_value)
            group["active_pairs"] += 1
            group["never_pairs"] += 1

        def group_status(group: dict[str, Any]) -> str:
            return (
                "attention"
                if (
                    group["near_expiry_pairs"]
                    or group["stale_pairs"]
                    or group["never_pairs"]
                    or group["future_pairs"]
                    or group["anomalous_pairs"]
                )
                else "ok"
            )

        rendered: list[dict[str, Any]] = []
        # ``None`` is explicitly sorted after textual values so the unobserved
        # source bucket has stable placement across calls and runtimes.
        for value in sorted(
            groups,
            key=lambda item: (item is None, "" if item is None else item),
        ):
            group = dict(groups[value])
            group["status"] = group_status(group)
            rendered.append(group)
        severity = {"ok": 0, "attention": 1, "degraded": 2}
        status = max(
            (group["status"] for group in rendered),
            key=severity.__getitem__,
            default="ok",
        )
        total_groups = len(rendered)
        return {
            "group_by": grouping,
            "evaluated_at": reference.isoformat(timespec="milliseconds"),
            "stale_after_seconds": age_limit,
            "status": status,
            "groups": rendered[:limit],
            "total_groups": total_groups,
            "has_more": total_groups > limit,
        }

    @staticmethod
    def _observation_snapshot_is_active(
        row: Any,
        record: WalletBalanceSnapshotRecord,
        subject_id: str,
    ) -> bool:
        """Validate one snapshot relationship and report its active lifecycle."""

        try:
            resource_subjects = (
                row["network_subject_id"],
                row["asset_subject_id"],
                row["address_subject_id"],
            )
            resource_statuses = (
                row["network_status"],
                row["asset_status"],
                row["address_status"],
            )
            asset_network_id = row["asset_network_id"]
            address_network_id = row["address_network_id"]
        except (KeyError, TypeError) as error:
            raise IntegrityError(
                f"wallet observation relationship is unavailable: {record.snapshot_id}"
            ) from error

        if record.subject_id != subject_id or any(
            owner != subject_id for owner in resource_subjects
        ):
            raise IntegrityError(f"wallet observation ownership mismatch: {record.snapshot_id}")
        if asset_network_id != record.network_id or address_network_id != record.network_id:
            raise IntegrityError(f"wallet observation reference mismatch: {record.snapshot_id}")
        if any(status not in {"active", "revoked"} for status in resource_statuses):
            raise IntegrityError(
                f"wallet observation resource lifecycle is invalid: {record.snapshot_id}"
            )

        network_status, asset_status, address_status = resource_statuses
        if network_status != "active" and (asset_status == "active" or address_status == "active"):
            raise IntegrityError(
                f"wallet observation active resource has revoked network: {record.snapshot_id}"
            )
        return resource_statuses == ("active", "active", "active")

    def _observation_health_connection(
        self,
        connection: Any,
        subject_id: str,
        *,
        age_limit: int,
        reference: datetime,
    ) -> dict[str, int | str | None]:
        """Compute observation health against a caller-owned read snapshot."""

        evaluated_at = reference.isoformat(timespec="milliseconds")
        stale_window = timedelta(seconds=age_limit)
        # Keep a zero-second window meaningful: an observation exactly at the
        # reference instant is fresh, while any positive age is stale.
        near_cutoff = (
            None if age_limit == 0 else timedelta(microseconds=(age_limit * 3 * 1_000_000) // 4)
        )

        # Count a bounded active target registry without materialising its
        # asset/address cross-product.  The unbounded history below remains a
        # cursor, and a configuration beyond this guard fails closed.
        active_pair_count = 0
        previous_target: tuple[str, str, str] | None = None
        for row in connection.execute(
            "SELECT a.network_id, a.asset_id, d.address_id "
            "FROM wallet_assets a "
            "JOIN wallet_addresses d ON d.subject_id = a.subject_id "
            "AND d.network_id = a.network_id AND d.status = 'active' "
            "JOIN wallet_networks n ON n.subject_id = a.subject_id "
            "AND n.network_id = a.network_id AND n.status = 'active' "
            "WHERE a.subject_id = ? AND a.status = 'active' "
            "ORDER BY a.network_id, a.asset_id, d.address_id LIMIT ?",
            (subject_id, WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS + 1),
        ):
            try:
                # Validate the durable values before composing the key.  In
                # particular, converting ``None`` (or another malformed SQL
                # value) to text first would turn it into the apparently valid
                # identifier ``"None"`` and could make a damaged target look
                # like a real pair.
                network_id = self._identifier(row["network_id"], "network identifier")
                asset_id = self._identifier(row["asset_id"], "asset identifier")
                address_id = self._identifier(row["address_id"], "address identifier")
                key = (network_id, asset_id, address_id)
            except (KeyError, TypeError, ValueError) as error:
                raise IntegrityError("wallet observation target identity is invalid") from error
            if key == previous_target:
                raise IntegrityError("wallet observation target is duplicated")
            active_pair_count += 1
            if active_pair_count > WALLET_BALANCE_OBSERVATION_MAX_ACTIVE_PAIRS:
                raise IntegrityError("wallet observation active pair limit is exceeded")
            previous_target = key

        observed_pairs = 0
        fresh_pairs = 0
        near_expiry_pairs = 0
        stale_pairs = 0
        future_pairs = 0
        max_age: int | None = None
        anomalous_pairs = 0
        current_key: tuple[str, str, str] | None = None
        previous_balance: int | None = None
        previous_balance_text: str | None = None
        previous_observed_at: str | None = None
        pair_anomalous = False

        history_query = (
            "SELECT s.*, n.subject_id AS network_subject_id, n.status AS network_status, "
            "a.subject_id AS asset_subject_id, a.network_id AS asset_network_id, "
            "a.status AS asset_status, d.subject_id AS address_subject_id, "
            "d.network_id AS address_network_id, d.status AS address_status "
            "FROM wallet_balance_snapshots s "
            "LEFT JOIN wallet_networks n ON n.network_id = s.network_id "
            "LEFT JOIN wallet_assets a ON a.asset_id = s.asset_id "
            "LEFT JOIN wallet_addresses d ON d.address_id = s.address_id "
            "WHERE s.subject_id = ? "
            "ORDER BY s.network_id, s.asset_id, s.address_id, "
            "s.observed_at DESC, s.snapshot_id DESC"
        )
        for row in connection.execute(history_query, (subject_id,)):
            # _balance_from_row validates the canonical decimal, timestamp,
            # ownership hash, and immutable row shape before it is used.
            record = self._balance_from_row(row)
            key = (record.network_id, record.asset_id, record.address_id)
            if not self._observation_snapshot_is_active(row, record, subject_id):
                continue

            if key != current_key:
                if pair_anomalous:
                    anomalous_pairs += 1
                current_key = key
                previous_balance = int(record.balance)
                previous_balance_text = record.balance
                previous_observed_at = record.observed_at
                pair_anomalous = False
                observed_pairs += 1
                if observed_pairs > active_pair_count:
                    raise IntegrityError("wallet observation pair count is inconsistent")

                parsed = self._persisted_observation_datetime(record.observed_at)
                delta = reference - parsed
                if delta < timedelta(0):
                    future_pairs += 1
                else:
                    # Both timestamps are canonical milliseconds, so this
                    # integer timedelta calculation avoids float rounding for
                    # dates far from the epoch while retaining the documented
                    # whole-second age projection.
                    age_seconds = delta.days * 86_400 + delta.seconds
                    max_age = age_seconds if max_age is None else max(max_age, age_seconds)
                    if delta > stale_window:
                        stale_pairs += 1
                    elif near_cutoff is not None and delta >= near_cutoff:
                        near_expiry_pairs += 1
                    else:
                        fresh_pairs += 1
                continue

            # Rows for one pair are contiguous and newest-first.  Same-time
            # disagreements are anomalous regardless of magnitude; otherwise
            # flag a symmetric >100% change without returning either value.
            current_balance = int(record.balance)
            if (
                previous_balance_text is not None
                and previous_observed_at is not None
                and record.observed_at == previous_observed_at
                and record.balance != previous_balance_text
            ):
                pair_anomalous = True
            elif previous_balance is not None:
                low = min(previous_balance, current_balance)
                high = max(previous_balance, current_balance)
                if (low == 0 and high != 0) or (low != 0 and high > low * 2):
                    pair_anomalous = True
            previous_balance = current_balance
            previous_balance_text = record.balance
            previous_observed_at = record.observed_at

        if pair_anomalous:
            anomalous_pairs += 1
        never_pairs = active_pair_count - observed_pairs
        if never_pairs < 0:
            raise IntegrityError("wallet observation never-pair count is invalid")

        status = (
            "attention"
            if (near_expiry_pairs or stale_pairs or never_pairs or future_pairs or anomalous_pairs)
            else "ok"
        )
        return {
            "status": status,
            "evaluated_at": evaluated_at,
            "stale_after_seconds": age_limit,
            "observed_pairs": observed_pairs,
            "fresh_pairs": fresh_pairs,
            "near_expiry_pairs": near_expiry_pairs,
            "stale_pairs": stale_pairs,
            "never_pairs": never_pairs,
            "future_pairs": future_pairs,
            "anomalous_pairs": anomalous_pairs,
            "max_age_seconds": max_age,
        }

    # Explicit aliases keep the vocabulary discoverable for callers while
    # preserving one implementation and one bounded response shape.
    balance_observation_health = observation_health
    balance_health = observation_health

    def status_summary(self, subject_id: str) -> dict[str, Any]:
        """Return a bounded public-state projection for operator diagnostics.

        The projection contains only aggregate lifecycle counts and timestamps.
        It never returns resource identifiers, balances, addresses, RPC origins,
        or acquisition lease material, and all result sets are consumed as
        cursors rather than materialized lists.
        """

        validate_subject_id(subject_id)
        age_limit = self._observation_age_limit(WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS)
        reference = self._observation_reference(None)
        resource_counts: dict[str, dict[str, int]] = {}
        with self.database.read_transaction() as connection:
            for key, table in (
                ("networks", "wallet_networks"),
                ("assets", "wallet_assets"),
                ("addresses", "wallet_addresses"),
            ):
                counts = {"active": 0, "revoked": 0}
                for row in connection.execute(
                    f"SELECT status, COUNT(*) AS count FROM {table} "
                    "WHERE subject_id = ? GROUP BY status ORDER BY status",
                    (subject_id,),
                ):
                    status = row["status"]
                    if status not in counts:
                        raise IntegrityError(f"wallet {key} status is invalid: {status}")
                    try:
                        count = int(row["count"])
                    except (TypeError, ValueError) as error:
                        raise IntegrityError(f"wallet {key} status count is invalid") from error
                    if count < 0:
                        raise IntegrityError(f"wallet {key} status count is invalid")
                    counts[status] = count
                counts["total"] = counts["active"] + counts["revoked"]
                resource_counts[key] = counts
            history = connection.execute(
                "SELECT COUNT(*) AS count, MAX(observed_at) AS latest_observed_at, "
                "MAX(created_at) AS latest_created_at FROM wallet_balance_snapshots "
                "WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            if history is None:
                raise IntegrityError("wallet balance history summary is unavailable")
            try:
                snapshot_count = int(history["count"])
            except (TypeError, ValueError) as error:
                raise IntegrityError("wallet balance history count is invalid") from error
            if snapshot_count < 0:
                raise IntegrityError("wallet balance history count is invalid")
            observation_health = self._observation_health_connection(
                connection,
                subject_id,
                age_limit=age_limit,
                reference=reference,
            )
        return {
            "subject_id": subject_id,
            "resources": resource_counts,
            "balance_history": {
                "snapshots": snapshot_count,
                "latest_observed_at": history["latest_observed_at"],
                "latest_created_at": history["latest_created_at"],
                "observation_health": observation_health,
            },
        }

    # Configuration callers can use the same vocabulary as the existing
    # resource stores without widening this domain into a signer interface.
    configure_network = register_network
    configure_asset = register_asset
    configure_address = register_address
    record_balance = record_balance_snapshot
    list_balances = latest_balances

    @staticmethod
    def _observation_group_by(value: object) -> str:
        if type(value) is not str or value not in {"network", "source"}:
            raise ValueError("wallet observation breakdown dimension is invalid")
        return value

    @classmethod
    def _observation_optional_identifier(cls, value: object, field: str) -> str | None:
        if value is None:
            return None
        return cls._identifier(value, field)

    @staticmethod
    def _observation_source(value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or _WALLET_OBSERVATION_SOURCE.fullmatch(value) is None:
            raise ValueError("wallet observation source is invalid")
        return value

    @staticmethod
    def _observation_group_limit(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= WALLET_BALANCE_OBSERVATION_MAX_GROUPS
        ):
            raise ValueError("wallet observation breakdown limit is invalid")
        return value

    @staticmethod
    def _observation_age_limit(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS
        ):
            raise ValueError("wallet observation age window is invalid")
        return value

    @staticmethod
    def _observation_reference(value: datetime | str | None) -> datetime:
        if value is None:
            value = utc_now()
        if isinstance(value, datetime):
            parsed = value
            if parsed.tzinfo is None:
                raise ValueError("wallet observation reference time must include a timezone")
            if parsed.microsecond % 1_000 != 0:
                raise ValueError("wallet observation reference time must use milliseconds")
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or canonical_timestamp(value) != value:
                    raise ValueError("wallet observation reference time is not canonical")
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("wallet observation reference time is invalid") from error
        else:
            raise TypeError("wallet observation reference time is invalid")
        try:
            return parsed.astimezone(UTC).replace(microsecond=(parsed.microsecond // 1_000) * 1_000)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("wallet observation reference time is invalid") from error

    @staticmethod
    def _persisted_observation_datetime(value: object) -> datetime:
        try:
            if (
                not isinstance(value, str)
                or not validate_timestamp(value)
                or canonical_timestamp(value) != value
            ):
                raise ValueError("non-canonical timestamp")
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("wallet observation timestamp is invalid") from error
        if parsed.tzinfo is None:
            raise IntegrityError("wallet observation timestamp is invalid")
        try:
            return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("wallet observation timestamp is invalid") from error

    @staticmethod
    def _require_operator(actor: object, action: str) -> None:
        if not isinstance(actor, str) or not actor.strip() or actor.strip() == "subject":
            raise PermissionError(f"only an operator can {action}")

    @staticmethod
    def _reason(value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("wallet revoke reason must be text")
        reason = value.strip()
        if not reason or len(reason) > 2_000:
            raise ValueError("wallet revoke reason is invalid")
        return reason

    @staticmethod
    def _limit(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _MAX_LIST_LIMIT
        ):
            raise ValueError("wallet list limit is invalid")
        return value

    @staticmethod
    def _history_limit(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE
        ):
            raise ValueError("wallet balance history page limit is invalid")
        return value

    @staticmethod
    def _identifier(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"wallet {field} is invalid")
        return value

    @classmethod
    def _encode_balance_history_cursor(
        cls,
        subject_id: str,
        network_id: str | None,
        asset_id: str | None,
        address_id: str | None,
        anchor: int,
        observed_at: str,
        snapshot_id: str,
    ) -> str:
        payload = {
            "version": WALLET_BALANCE_HISTORY_CURSOR_VERSION,
            "subject_id": subject_id,
            "network_id": network_id,
            "asset_id": asset_id,
            "address_id": address_id,
            "anchor": anchor,
            "observed_at": observed_at,
            "snapshot_id": snapshot_id,
        }
        encoded = (
            base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8"))
            .rstrip(b"=")
            .decode("ascii")
        )
        if len(encoded) > WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH:
            raise IntegrityError("wallet balance history cursor exceeds the configured bound")
        return encoded

    @classmethod
    def _decode_balance_history_cursor(cls, cursor: object) -> _BalanceHistoryCursor:
        if (
            type(cursor) is not str
            or not cursor
            or len(cursor) > WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH
            or any(character not in _URLSAFE_CURSOR_ALPHABET for character in cursor)
        ):
            raise ValueError("wallet balance history cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
            payload = strict_json_loads(raw)
        except (binascii.Error, UnicodeError, ValueError, TypeError) as error:
            raise ValueError("wallet balance history cursor is invalid") from error
        required = {
            "version",
            "subject_id",
            "network_id",
            "asset_id",
            "address_id",
            "anchor",
            "observed_at",
            "snapshot_id",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("wallet balance history cursor is invalid")
        try:
            if payload["version"] != WALLET_BALANCE_HISTORY_CURSOR_VERSION:
                raise ValueError("cursor version is invalid")
            subject_id = payload["subject_id"]
            validate_subject_id(subject_id)
            filters: list[str | None] = []
            for field in ("network_id", "asset_id", "address_id"):
                value = payload[field]
                if value is not None:
                    cls._identifier(value, f"{field} in cursor")
                filters.append(value)
            anchor = payload["anchor"]
            if (
                isinstance(anchor, bool)
                or not isinstance(anchor, int)
                or not 0 <= anchor <= SQLITE_INT64_MAX
            ):
                raise ValueError("cursor anchor is invalid")
            observed_at = payload["observed_at"]
            if (
                not isinstance(observed_at, str)
                or len(observed_at) > 64
                or canonical_timestamp(observed_at) != observed_at
            ):
                raise ValueError("cursor timestamp is invalid")
            snapshot_id = payload["snapshot_id"]
            cls._identifier(snapshot_id, "snapshot identifier in cursor")
            canonical = (
                base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8"))
                .rstrip(b"=")
                .decode("ascii")
            )
            if canonical != cursor:
                raise ValueError("cursor encoding is not canonical")
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("wallet balance history cursor is invalid") from error
        return _BalanceHistoryCursor(
            str(subject_id),
            filters[0],
            filters[1],
            filters[2],
            int(anchor),
            str(observed_at),
            str(snapshot_id),
        )

    @classmethod
    def _balance_filters(
        cls,
        subject_id: str,
        network_id: str | None,
        asset_id: str | None,
        address_id: str | None,
    ) -> tuple[str, list[object]]:
        validate_subject_id(subject_id)
        clauses = ["wallet_balance_snapshots.subject_id = ?"]
        values: list[object] = [subject_id]
        for column, value, label in (
            ("network_id", network_id, "network identifier"),
            ("asset_id", asset_id, "asset identifier"),
            ("address_id", address_id, "address identifier"),
        ):
            if value is not None:
                cls._identifier(value, label)
                clauses.append(f"wallet_balance_snapshots.{column} = ?")
                values.append(value)
        return " AND ".join(clauses), values

    @staticmethod
    def _append_audit(
        connection: Any,
        subject_id: str,
        action: str,
        actor: str,
        payload: Mapping[str, object],
    ) -> str:
        audit_id = new_id("audit")
        connection.execute(
            """INSERT INTO audit_records(
                audit_id, subject_id, action, actor, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (audit_id, subject_id, action, actor, canonical_json(dict(payload)), utc_now()),
        )
        return audit_id

    @staticmethod
    def _network_registration_audit_payload(
        network_id: str, proposal: WalletNetworkInput
    ) -> dict[str, object]:
        return {
            "network_id": network_id,
            "label": proposal.label,
            "chain_family": proposal.chain_family,
            "chain_id": proposal.chain_id,
        }

    @staticmethod
    def _asset_registration_audit_payload(
        asset_id: str, proposal: WalletAssetInput
    ) -> dict[str, object]:
        return {
            "asset_id": asset_id,
            "network_id": proposal.network_id,
            "asset_type": proposal.asset_type,
            "contract_address": proposal.contract_address,
        }

    @staticmethod
    def _address_registration_audit_payload(
        address_id: str, proposal: WalletAddressInput
    ) -> dict[str, object]:
        return {
            "address_id": address_id,
            "network_id": proposal.network_id,
            "address": proposal.address,
        }

    @staticmethod
    def _network_hash(
        network_id: str,
        subject_id: str,
        label: str,
        chain_family: str,
        chain_id: int,
        native_symbol: str,
        rpc_url: str | None,
        status: str,
        created_at: str,
        revoked_at: str | None,
        revoke_reason: str | None,
        created_audit_id: str,
        revoked_audit_id: str | None,
    ) -> str:
        return content_hash(
            {
                "network_id": network_id,
                "subject_id": subject_id,
                "label": label,
                "chain_family": chain_family,
                "chain_id": chain_id,
                "native_symbol": native_symbol,
                "rpc_url": rpc_url,
                "status": status,
                "created_at": created_at,
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
                "created_audit_id": created_audit_id,
                "revoked_audit_id": revoked_audit_id,
            }
        )

    @staticmethod
    def _asset_hash(
        asset_id: str,
        subject_id: str,
        network_id: str,
        asset_type: str,
        contract_address: str | None,
        name: str,
        symbol: str,
        decimals: int,
        status: str,
        created_at: str,
        revoked_at: str | None,
        revoke_reason: str | None,
        created_audit_id: str,
        revoked_audit_id: str | None,
    ) -> str:
        return content_hash(
            {
                "asset_id": asset_id,
                "subject_id": subject_id,
                "network_id": network_id,
                "asset_type": asset_type,
                "contract_address": contract_address,
                "name": name,
                "symbol": symbol,
                "decimals": decimals,
                "status": status,
                "created_at": created_at,
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
                "created_audit_id": created_audit_id,
                "revoked_audit_id": revoked_audit_id,
            }
        )

    @staticmethod
    def _address_hash(
        address_id: str,
        subject_id: str,
        network_id: str,
        label: str,
        address: str,
        purpose: str,
        status: str,
        created_at: str,
        revoked_at: str | None,
        revoke_reason: str | None,
        created_audit_id: str,
        revoked_audit_id: str | None,
    ) -> str:
        return content_hash(
            {
                "address_id": address_id,
                "subject_id": subject_id,
                "network_id": network_id,
                "label": label,
                "address": address,
                "purpose": purpose,
                "status": status,
                "created_at": created_at,
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
                "created_audit_id": created_audit_id,
                "revoked_audit_id": revoked_audit_id,
            }
        )

    @staticmethod
    def _balance_hash(
        snapshot_id: str,
        subject_id: str,
        network_id: str,
        asset_id: str,
        address_id: str,
        balance: str,
        source: str,
        observed_at: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "snapshot_id": snapshot_id,
                "subject_id": subject_id,
                "network_id": network_id,
                "asset_id": asset_id,
                "address_id": address_id,
                "balance": balance,
                "source": source,
                "observed_at": observed_at,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _revision_hash(
        resource_type: str,
        resource_id: str,
        subject_id: str,
        revision_number: int,
        status: str,
        reason: str,
        audit_id: str,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "subject_id": subject_id,
                "revision_number": revision_number,
                "status": status,
                "reason": reason,
                "audit_id": audit_id,
                "created_at": created_at,
            }
        )

    @classmethod
    def _insert_revision(
        cls,
        connection: Any,
        resource_type: str,
        resource_id: str,
        subject_id: str,
        revision_number: int,
        status: str,
        reason: str,
        audit_id: str,
        created_at: str,
    ) -> None:
        _table, revision_table, id_column = _RESOURCE_META[resource_type]
        connection.execute(
            f"""INSERT INTO {revision_table}(
                revision_id, {id_column}, subject_id, revision_number, status,
                reason, audit_id, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id(f"wallet{resource_type}rev"),
                resource_id,
                subject_id,
                revision_number,
                status,
                reason,
                audit_id,
                cls._revision_hash(
                    resource_type,
                    resource_id,
                    subject_id,
                    revision_number,
                    status,
                    reason,
                    audit_id,
                    created_at,
                ),
                created_at,
            ),
        )

    @staticmethod
    def _network_row(connection: Any, network_id: str, *, subject_id: str) -> Any:
        WalletStore._identifier(network_id, "network identifier")
        row = connection.execute(
            "SELECT * FROM wallet_networks WHERE network_id = ? AND subject_id = ?",
            (network_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"wallet network not found: {network_id}")
        return row

    @staticmethod
    def _asset_row(connection: Any, asset_id: str, *, subject_id: str) -> Any:
        WalletStore._identifier(asset_id, "asset identifier")
        row = connection.execute(
            "SELECT * FROM wallet_assets WHERE asset_id = ? AND subject_id = ?",
            (asset_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"wallet asset not found: {asset_id}")
        return row

    @staticmethod
    def _address_row(connection: Any, address_id: str, *, subject_id: str) -> Any:
        WalletStore._identifier(address_id, "address identifier")
        row = connection.execute(
            "SELECT * FROM wallet_addresses WHERE address_id = ? AND subject_id = ?",
            (address_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"wallet address not found: {address_id}")
        return row

    @classmethod
    def _network_from_row(cls, row: Any) -> WalletNetworkRecord:
        network_id = row["network_id"]
        subject_id = row["subject_id"]
        chain_id_raw = row["chain_id"]
        try:
            if isinstance(chain_id_raw, bool) or not isinstance(chain_id_raw, int):
                raise ValueError("network chain identifier is invalid")
            proposal = WalletNetworkInput(
                label=row["label"],
                chain_family=row["chain_family"],
                chain_id=chain_id_raw,
                native_symbol=row["native_symbol"],
                rpc_url=row["rpc_url"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet network durable state is invalid: {network_id}"
            ) from error
        try:
            cls._identifier(network_id, "network identifier")
            validate_subject_id(subject_id)
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet network durable identity is invalid: {network_id}"
            ) from error
        status, created_at, revoked_at, revoke_reason, created_audit_id, revoked_audit_id = (
            cls._lifecycle_values(row, network_id)
        )
        if (
            row["label"] != proposal.label
            or row["chain_family"] != proposal.chain_family
            or row["chain_id"] != proposal.chain_id
            or row["native_symbol"] != proposal.native_symbol
            or row["rpc_url"] != proposal.rpc_url
        ):
            raise IntegrityError(f"wallet network state is non-canonical: {network_id}")
        expected = cls._network_hash(
            network_id,
            subject_id,
            proposal.label,
            proposal.chain_family,
            proposal.chain_id,
            proposal.native_symbol,
            proposal.rpc_url,
            status,
            created_at,
            revoked_at,
            revoke_reason,
            created_audit_id,
            revoked_audit_id,
        )
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet network state hash mismatch: {network_id}")
        return WalletNetworkRecord(
            network_id,
            subject_id,
            proposal.label,
            proposal.chain_family,
            proposal.chain_id,
            proposal.native_symbol,
            proposal.rpc_url,
            status,
            created_at,
            revoked_at,
            revoke_reason,
        )

    @classmethod
    def _asset_from_row(cls, row: Any) -> WalletAssetRecord:
        asset_id = row["asset_id"]
        subject_id = row["subject_id"]
        decimals_raw = row["decimals"]
        try:
            if isinstance(decimals_raw, bool) or not isinstance(decimals_raw, int):
                raise ValueError("asset decimals are invalid")
            proposal = WalletAssetInput(
                network_id=row["network_id"],
                asset_type=row["asset_type"],
                contract_address=row["contract_address"],
                name=row["name"],
                symbol=row["symbol"],
                decimals=decimals_raw,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"wallet asset durable state is invalid: {asset_id}") from error
        try:
            cls._identifier(asset_id, "asset identifier")
            validate_subject_id(subject_id)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"wallet asset durable identity is invalid: {asset_id}") from error
        status, created_at, revoked_at, revoke_reason, created_audit_id, revoked_audit_id = (
            cls._lifecycle_values(row, asset_id)
        )
        if (
            row["network_id"] != proposal.network_id
            or row["asset_type"] != proposal.asset_type
            or row["contract_address"] != proposal.contract_address
            or row["name"] != proposal.name
            or row["symbol"] != proposal.symbol
            or row["decimals"] != proposal.decimals
        ):
            raise IntegrityError(f"wallet asset state is non-canonical: {asset_id}")
        expected = cls._asset_hash(
            asset_id,
            subject_id,
            proposal.network_id,
            proposal.asset_type,
            proposal.contract_address,
            proposal.name,
            proposal.symbol,
            proposal.decimals,
            status,
            created_at,
            revoked_at,
            revoke_reason,
            created_audit_id,
            revoked_audit_id,
        )
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet asset state hash mismatch: {asset_id}")
        return WalletAssetRecord(
            asset_id,
            subject_id,
            proposal.network_id,
            proposal.asset_type,
            proposal.contract_address,
            proposal.name,
            proposal.symbol,
            proposal.decimals,
            status,
            created_at,
            revoked_at,
            revoke_reason,
        )

    @classmethod
    def _address_from_row(cls, row: Any) -> WalletAddressRecord:
        address_id = row["address_id"]
        subject_id = row["subject_id"]
        try:
            proposal = WalletAddressInput(
                network_id=row["network_id"],
                label=row["label"],
                address=row["address"],
                purpose=row["purpose"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet address durable state is invalid: {address_id}"
            ) from error
        try:
            cls._identifier(address_id, "address identifier")
            validate_subject_id(subject_id)
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet address durable identity is invalid: {address_id}"
            ) from error
        status, created_at, revoked_at, revoke_reason, created_audit_id, revoked_audit_id = (
            cls._lifecycle_values(row, address_id)
        )
        if (
            row["network_id"] != proposal.network_id
            or row["label"] != proposal.label
            or row["address"] != proposal.address
            or row["purpose"] != proposal.purpose
        ):
            raise IntegrityError(f"wallet address state is non-canonical: {address_id}")
        expected = cls._address_hash(
            address_id,
            subject_id,
            proposal.network_id,
            proposal.label,
            proposal.address,
            proposal.purpose,
            status,
            created_at,
            revoked_at,
            revoke_reason,
            created_audit_id,
            revoked_audit_id,
        )
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet address state hash mismatch: {address_id}")
        return WalletAddressRecord(
            address_id,
            subject_id,
            proposal.network_id,
            proposal.label,
            proposal.address,
            proposal.purpose,
            status,
            created_at,
            revoked_at,
            revoke_reason,
        )

    @classmethod
    def _balance_from_row(cls, row: Any) -> WalletBalanceSnapshotRecord:
        snapshot_id = row["snapshot_id"]
        subject_id = row["subject_id"]
        try:
            proposal = WalletBalanceSnapshotInput(
                asset_id=row["asset_id"],
                address_id=row["address_id"],
                balance=row["balance"],
                source=row["source"],
                observed_at=row["observed_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet balance snapshot durable state is invalid: {snapshot_id}"
            ) from error
        observed_at = proposal.observed_at
        if observed_at is None:
            raise IntegrityError(f"wallet balance snapshot time is invalid: {snapshot_id}")
        try:
            cls._identifier(snapshot_id, "balance snapshot identifier")
            cls._identifier(row["network_id"], "network identifier")
            validate_subject_id(subject_id)
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet balance snapshot durable identity is invalid: {snapshot_id}"
            ) from error
        try:
            created_at = row["created_at"]
            if canonical_timestamp(created_at) != created_at:
                raise ValueError("wallet balance snapshot creation time is not canonical")
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet balance snapshot time is invalid: {snapshot_id}"
            ) from error
        if (
            row["asset_id"] != proposal.asset_id
            or row["address_id"] != proposal.address_id
            or row["balance"] != proposal.balance
            or row["source"] != proposal.source
            or row["observed_at"] != observed_at
        ):
            raise IntegrityError(f"wallet balance snapshot is non-canonical: {snapshot_id}")
        expected = cls._balance_hash(
            snapshot_id,
            subject_id,
            row["network_id"],
            proposal.asset_id,
            proposal.address_id,
            proposal.balance,
            proposal.source,
            observed_at,
            row["created_at"],
        )
        if row["state_hash"] != expected:
            raise IntegrityError(f"wallet balance snapshot hash mismatch: {snapshot_id}")
        return WalletBalanceSnapshotRecord(
            snapshot_id,
            subject_id,
            row["network_id"],
            proposal.asset_id,
            proposal.address_id,
            proposal.balance,
            proposal.source,
            observed_at,
            row["created_at"],
        )

    @classmethod
    def _lifecycle_values(
        cls, row: Any, resource_id: object
    ) -> tuple[str, str, str | None, str | None, str, str | None]:
        status = row["status"]
        created_at = row["created_at"]
        revoked_at = row["revoked_at"]
        revoke_reason = row["revoke_reason"]
        created_audit_id = row["created_audit_id"]
        revoked_audit_id = row["revoked_audit_id"]
        try:
            cls._identifier(created_audit_id, "creation audit identifier")
            if status not in {"active", "revoked"} or not validate_timestamp(created_at):
                raise ValueError("wallet lifecycle is invalid")
            if status == "active":
                if (
                    revoked_at is not None
                    or revoke_reason is not None
                    or revoked_audit_id is not None
                ):
                    raise ValueError("active wallet record has revoke metadata")
            elif (
                not validate_timestamp(revoked_at)
                or not isinstance(revoke_reason, str)
                or not revoke_reason.strip()
                or len(revoke_reason) > 2_000
                or not isinstance(revoked_audit_id, str)
                or not revoked_audit_id
            ):
                raise ValueError("revoked wallet lifecycle is invalid")
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"wallet lifecycle is invalid: {resource_id}") from error
        return status, created_at, revoked_at, revoke_reason, created_audit_id, revoked_audit_id

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        """Verify ownership, lifecycle evidence, and snapshot relationships."""

        validate_subject_id(subject_id)
        with self.database.read_transaction() as connection:
            networks: dict[str, _WalletResourceState] = {}
            for row in connection.execute(
                "SELECT * FROM wallet_networks WHERE subject_id = ? "
                "ORDER BY created_at, network_id",
                (subject_id,),
            ):
                network_record = self._network_from_row(row)
                networks[network_record.network_id] = _WalletResourceState(
                    network_record,
                    row["created_audit_id"],
                    row["revoked_audit_id"],
                )
                self._verify_resource_audits(connection, "network", row, network_record)
            self._verify_revisions(connection, "network", subject_id, networks)

            assets: dict[str, _WalletResourceState] = {}
            for row in connection.execute(
                """SELECT a.*, n.subject_id AS parent_subject_id, n.status AS parent_status
                   FROM wallet_assets a
                   LEFT JOIN wallet_networks n ON n.network_id = a.network_id
                   WHERE a.subject_id = ? OR n.subject_id = ?
                   ORDER BY a.created_at, a.asset_id""",
                (subject_id, subject_id),
            ):
                asset_record = self._asset_from_row(row)
                if row["subject_id"] != subject_id or row["parent_subject_id"] != subject_id:
                    raise IntegrityError(
                        f"wallet asset ownership mismatch: {asset_record.asset_id}"
                    )
                network = networks.get(asset_record.network_id)
                if network is None:
                    raise IntegrityError(
                        f"wallet asset network is missing: {asset_record.asset_id}"
                    )
                if (
                    asset_record.asset_type == "native"
                    and asset_record.symbol != network.record.native_symbol
                ):
                    raise IntegrityError(
                        f"wallet native asset symbol mismatch: {asset_record.asset_id}"
                    )
                if asset_record.status == "active" and network.record.status != "active":
                    raise IntegrityError(
                        f"active wallet asset has revoked network: {asset_record.asset_id}"
                    )
                assets[asset_record.asset_id] = _WalletResourceState(
                    asset_record,
                    row["created_audit_id"],
                    row["revoked_audit_id"],
                )
                self._verify_resource_audits(connection, "asset", row, asset_record)
            self._verify_revisions(connection, "asset", subject_id, assets)

            addresses: dict[str, _WalletResourceState] = {}
            for row in connection.execute(
                """SELECT a.*, n.subject_id AS parent_subject_id, n.status AS parent_status
                   FROM wallet_addresses a
                   LEFT JOIN wallet_networks n ON n.network_id = a.network_id
                   WHERE a.subject_id = ? OR n.subject_id = ?
                   ORDER BY a.created_at, a.address_id""",
                (subject_id, subject_id),
            ):
                address_record = self._address_from_row(row)
                if row["subject_id"] != subject_id or row["parent_subject_id"] != subject_id:
                    raise IntegrityError(
                        f"wallet address ownership mismatch: {address_record.address_id}"
                    )
                network = networks.get(address_record.network_id)
                if network is None:
                    raise IntegrityError(
                        f"wallet address network is missing: {address_record.address_id}"
                    )
                if address_record.status == "active" and network.record.status != "active":
                    raise IntegrityError(
                        f"active wallet address has revoked network: {address_record.address_id}"
                    )
                addresses[address_record.address_id] = _WalletResourceState(
                    address_record,
                    row["created_audit_id"],
                    row["revoked_audit_id"],
                )
                self._verify_resource_audits(connection, "address", row, address_record)
            self._verify_revisions(connection, "address", subject_id, addresses)

            snapshots = 0
            for row in connection.execute(
                """SELECT s.*, n.subject_id AS network_subject_id,
                          a.subject_id AS asset_subject_id, d.subject_id AS address_subject_id,
                          a.network_id AS asset_network_id, d.network_id AS address_network_id
                   FROM wallet_balance_snapshots s
                   LEFT JOIN wallet_networks n ON n.network_id = s.network_id
                   LEFT JOIN wallet_assets a ON a.asset_id = s.asset_id
                   LEFT JOIN wallet_addresses d ON d.address_id = s.address_id
                   WHERE s.subject_id = ? OR n.subject_id = ? OR a.subject_id = ?
                      OR d.subject_id = ?
                   ORDER BY s.observed_at, s.snapshot_id""",
                (subject_id, subject_id, subject_id, subject_id),
            ):
                snapshot_record = self._balance_from_row(row)
                if any(
                    row[field] != subject_id
                    for field in (
                        "subject_id",
                        "network_subject_id",
                        "asset_subject_id",
                        "address_subject_id",
                    )
                ):
                    raise IntegrityError(
                        f"wallet balance ownership mismatch: {snapshot_record.snapshot_id}"
                    )
                if (
                    snapshot_record.network_id not in networks
                    or snapshot_record.asset_id not in assets
                    or snapshot_record.address_id not in addresses
                    or row["asset_network_id"] != snapshot_record.network_id
                    or row["address_network_id"] != snapshot_record.network_id
                ):
                    raise IntegrityError(
                        f"wallet balance reference mismatch: {snapshot_record.snapshot_id}"
                    )
                snapshots += 1
        details = {
            "wallet_networks": len(networks),
            "wallet_network_revisions": self._revision_count(networks),
            "wallet_assets": len(assets),
            "wallet_asset_revisions": self._revision_count(assets),
            "wallet_addresses": len(addresses),
            "wallet_address_revisions": self._revision_count(addresses),
            "wallet_balance_snapshots": snapshots,
        }
        # Keep the historical empty-wallet result stable, while extending
        # integrity coverage as soon as the durable acquisition ledger exists.
        from .acquisition import WalletBalanceAcquisitionLedger

        acquisition = WalletBalanceAcquisitionLedger(self.database)
        acquisition_details = acquisition.verify_integrity(subject_id)
        if acquisition_details["wallet_balance_acquisition_runs"]:
            details.update(acquisition_details)
        return details

    @staticmethod
    def _revision_count(resources: Mapping[str, _WalletResourceState]) -> int:
        return sum(1 if state.record.status == "active" else 2 for state in resources.values())

    def _verify_resource_audits(
        self, connection: Any, resource_type: str, row: Any, record: Any
    ) -> None:
        id_field = _RESOURCE_META[resource_type][2]
        resource_id = getattr(record, id_field)
        self._verify_audit(
            connection,
            row["created_audit_id"],
            record.subject_id,
            f"wallet_{resource_type}_registered",
            self._registration_audit_payload(resource_type, resource_id, record),
        )
        if record.status == "revoked":
            self._verify_audit(
                connection,
                row["revoked_audit_id"],
                record.subject_id,
                f"wallet_{resource_type}_revoked",
                {id_field: resource_id, "reason": record.revoke_reason},
            )

    @staticmethod
    def _registration_audit_payload(
        resource_type: str, resource_id: str, record: Any
    ) -> dict[str, object]:
        if resource_type == "network":
            return {
                "network_id": resource_id,
                "label": record.label,
                "chain_family": record.chain_family,
                "chain_id": record.chain_id,
            }
        if resource_type == "asset":
            return {
                "asset_id": resource_id,
                "network_id": record.network_id,
                "asset_type": record.asset_type,
                "contract_address": record.contract_address,
            }
        return {
            "address_id": resource_id,
            "network_id": record.network_id,
            "address": record.address,
        }

    @staticmethod
    def _verify_audit(
        connection: Any,
        audit_id: object,
        subject_id: str,
        action: str,
        expected_payload: Mapping[str, object],
    ) -> None:
        if not isinstance(audit_id, str) or not audit_id:
            raise IntegrityError("wallet lifecycle audit identifier is invalid")
        row = connection.execute(
            "SELECT * FROM audit_records WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        if (
            row is None
            or row["subject_id"] != subject_id
            or row["action"] != action
            or not isinstance(row["actor"], str)
            or not row["actor"].strip()
            or row["actor"].strip() == "subject"
            or not validate_timestamp(row["occurred_at"])
            or not isinstance(row["payload_json"], str)
        ):
            raise IntegrityError(f"wallet lifecycle audit is invalid: {audit_id}")
        try:
            payload = strict_json_loads(row["payload_json"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"wallet lifecycle audit payload is invalid: {audit_id}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload != dict(expected_payload)
            or canonical_json(payload) != row["payload_json"]
        ):
            raise IntegrityError(f"wallet lifecycle audit payload mismatch: {audit_id}")

    def _verify_revisions(
        self,
        connection: Any,
        resource_type: str,
        subject_id: str,
        resources: Mapping[str, _WalletResourceState],
    ) -> None:
        table, revision_table, id_field = _RESOURCE_META[resource_type]
        rows = connection.execute(
            f"""SELECT r.*, p.subject_id AS parent_subject_id
                FROM {revision_table} r
                LEFT JOIN {table} p ON p.{id_field} = r.{id_field}
                WHERE r.subject_id = ? OR p.subject_id = ?
                ORDER BY r.{id_field}, r.revision_number, r.revision_id""",
            (subject_id, subject_id),
        )
        revision_counts: dict[str, int] = {}
        current_id: str | None = None
        current_count = 0

        def finish(resource_id: str | None, count: int) -> None:
            if resource_id is None:
                return
            state = resources[resource_id]
            expected = 1 if state.record.status == "active" else 2
            if count != expected:
                raise IntegrityError(
                    f"wallet {resource_type} revision history mismatch: {resource_id}"
                )
            revision_counts[resource_id] = count

        for row in rows:
            resource_id = row[id_field]
            if (
                not isinstance(resource_id, str)
                or not resource_id
                or row["subject_id"] != subject_id
                or row["parent_subject_id"] != subject_id
                or resource_id not in resources
            ):
                raise IntegrityError(f"wallet {resource_type} revision ownership mismatch")
            if current_id != resource_id:
                finish(current_id, current_count)
                current_id = resource_id
                current_count = 0
            current_count += 1
            state = resources[resource_id]
            record = state.record
            position = current_count
            expected_statuses = ("active",) if record.status == "active" else ("active", "revoked")
            revision_number = row["revision_number"]
            if (
                isinstance(revision_number, bool)
                or not isinstance(revision_number, int)
                or revision_number != position
                or position > len(expected_statuses)
                or row["status"] != expected_statuses[position - 1]
                or not isinstance(row["reason"], str)
                or not row["reason"].strip()
                or not validate_timestamp(row["created_at"])
                or not isinstance(row["audit_id"], str)
                or not row["audit_id"]
                or not isinstance(row["revision_id"], str)
                or not row["revision_id"]
            ):
                raise IntegrityError(f"wallet {resource_type} revision is invalid: {resource_id}")
            expected_hash = self._revision_hash(
                resource_type,
                resource_id,
                subject_id,
                revision_number,
                row["status"],
                row["reason"],
                row["audit_id"],
                row["created_at"],
            )
            if row["state_hash"] != expected_hash:
                raise IntegrityError(
                    f"wallet {resource_type} revision hash mismatch: {resource_id}"
                )
            if position == 1:
                if (
                    row["created_at"] != record.created_at
                    or row["reason"] != f"operator registered wallet {resource_type}"
                    or row["audit_id"] != state.created_audit_id
                ):
                    raise IntegrityError(
                        f"wallet {resource_type} creation revision mismatch: {resource_id}"
                    )
            elif (
                row["created_at"] != record.revoked_at
                or row["reason"] != record.revoke_reason
                or row["audit_id"] != state.revoked_audit_id
            ):
                raise IntegrityError(
                    f"wallet {resource_type} revoke revision mismatch: {resource_id}"
                )
        finish(current_id, current_count)
        for resource_id in resources:
            if resource_id not in revision_counts:
                raise IntegrityError(
                    f"wallet {resource_type} revision history mismatch: {resource_id}"
                )
