"""Safe, operator-controlled migration policy persistence."""

# SQL statements are intentionally kept readable as one statement per query.
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Literal, cast

from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

ApprovalMode = Literal["disabled", "manual", "policy_auto", "emergency_recovery"]
WalletMode = Literal["external_signer_rebind", "local_wallet_transfer", "disabled"]
_APPROVAL_MODES = frozenset({"disabled", "manual", "policy_auto", "emergency_recovery"})
_WALLET_MODES = frozenset({"external_signer_rebind", "local_wallet_transfer", "disabled"})


class MigrationPolicyConflictError(RuntimeError):
    """The policy revision changed before an operator update was committed."""


@dataclass(frozen=True)
class MigrationPolicy:
    subject_id: str
    enabled: bool = False
    approval_mode: ApprovalMode = "disabled"
    emergency_recovery_enabled: bool = False
    local_wallet_transfer_enabled: bool = False
    wallet_mode: WalletMode = "external_signer_rebind"
    allowed_target_ids: tuple[str, ...] = ()
    allowed_regions: tuple[str, ...] = ()
    min_free_bytes: int = 0
    max_cost_microusd: int = 0
    max_downtime_seconds: int = 3600
    maintenance_window_start_minute: int = 0
    maintenance_window_duration_minutes: int = 1440
    trust_level: int = 3
    rejection_cooldown_seconds: int = 604800
    proposal_expiry_seconds: int = 86400
    revision: int = 1
    updated_at: str = ""
    state_hash: str = ""

    @classmethod
    def default(cls, subject_id: str) -> MigrationPolicy:
        policy = cls(subject_id=validate_subject_id(subject_id), updated_at=utc_now())
        return replace(policy, state_hash=policy.compute_state_hash())

    @classmethod
    def from_record(cls, row: Any) -> MigrationPolicy:
        def json_tuple(name: str) -> tuple[str, ...]:
            try:
                value = json.loads(row[name])
            except (TypeError, ValueError) as error:
                raise ValueError(f"migration policy {name} is invalid") from error
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"migration policy {name} must be a list of strings")
            return tuple(dict.fromkeys(item.strip() for item in value))

        policy = cls(
            subject_id=validate_subject_id(str(row["subject_id"])),
            enabled=bool(row["enabled"]),
            approval_mode=cast(ApprovalMode, str(row["approval_mode"])),
            emergency_recovery_enabled=bool(row["emergency_recovery_enabled"]),
            local_wallet_transfer_enabled=bool(row["local_wallet_transfer_enabled"]),
            wallet_mode=cast(WalletMode, str(row["wallet_mode"])),
            allowed_target_ids=json_tuple("allowed_target_ids_json"),
            allowed_regions=json_tuple("allowed_regions_json"),
            min_free_bytes=int(row["min_free_bytes"]),
            max_cost_microusd=int(row["max_cost_microusd"]),
            max_downtime_seconds=int(row["max_downtime_seconds"]),
            maintenance_window_start_minute=int(row["maintenance_window_start_minute"]),
            maintenance_window_duration_minutes=int(row["maintenance_window_duration_minutes"]),
            trust_level=int(row["trust_level"]),
            rejection_cooldown_seconds=int(row["rejection_cooldown_seconds"]),
            proposal_expiry_seconds=int(row["proposal_expiry_seconds"]),
            revision=int(row["revision"]),
            updated_at=str(row["updated_at"]),
            state_hash=str(row["state_hash"]),
        )
        policy.validate()
        if policy.state_hash != policy.compute_state_hash():
            raise ValueError("migration policy state hash mismatch")
        return policy

    def _state_values(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "subject_id",
                "enabled",
                "approval_mode",
                "emergency_recovery_enabled",
                "local_wallet_transfer_enabled",
                "wallet_mode",
                "allowed_target_ids",
                "allowed_regions",
                "min_free_bytes",
                "max_cost_microusd",
                "max_downtime_seconds",
                "maintenance_window_start_minute",
                "maintenance_window_duration_minutes",
                "trust_level",
                "rejection_cooldown_seconds",
                "proposal_expiry_seconds",
                "revision",
                "updated_at",
            )
        }

    def compute_state_hash(self) -> str:
        return content_hash(self._state_values())

    def validate(self) -> None:
        validate_subject_id(self.subject_id)
        if type(self.enabled) is not bool:
            raise TypeError("migration enabled must be boolean")
        if self.approval_mode not in _APPROVAL_MODES:
            raise ValueError("invalid migration approval mode")
        if self.wallet_mode not in _WALLET_MODES:
            raise ValueError("invalid migration wallet mode")
        if (
            type(self.emergency_recovery_enabled) is not bool
            or type(self.local_wallet_transfer_enabled) is not bool
        ):
            raise TypeError("migration policy flags must be boolean")
        if not self.enabled and self.approval_mode not in {"disabled", "manual"}:
            raise ValueError("disabled migration cannot use automatic approval")
        if self.enabled and self.approval_mode == "disabled":
            raise ValueError("enabled migration requires an approval mode")
        if self.approval_mode == "policy_auto" and not self.allowed_target_ids:
            raise ValueError("policy_auto requires a non-empty target allowlist")
        if self.approval_mode == "emergency_recovery" and not self.emergency_recovery_enabled:
            raise ValueError("emergency recovery approval requires explicit enablement")
        if self.wallet_mode == "local_wallet_transfer" and not (
            self.enabled and self.local_wallet_transfer_enabled
        ):
            raise ValueError("local wallet transfer requires explicit local wallet opt-in")
        if len(self.allowed_target_ids) > 256 or any(
            not isinstance(item, str) or not item.strip() for item in self.allowed_target_ids
        ):
            raise ValueError("target allowlist is invalid")
        if len(self.allowed_regions) > 64 or any(
            not isinstance(item, str) or not item.strip() for item in self.allowed_regions
        ):
            raise ValueError("migration regions are invalid")
        bounds = (
            ("min_free_bytes", self.min_free_bytes, 0, 10**15),
            ("max_cost_microusd", self.max_cost_microusd, 0, 10**12),
            ("max_downtime_seconds", self.max_downtime_seconds, 0, 604800),
            ("maintenance_window_start_minute", self.maintenance_window_start_minute, 0, 1439),
            (
                "maintenance_window_duration_minutes",
                self.maintenance_window_duration_minutes,
                1,
                1440,
            ),
            ("trust_level", self.trust_level, 1, 5),
            ("rejection_cooldown_seconds", self.rejection_cooldown_seconds, 0, 31536000),
            ("proposal_expiry_seconds", self.proposal_expiry_seconds, 300, 604800),
            ("revision", self.revision, 1, 2**63 - 1),
        )
        for name, value, lower, upper in bounds:
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} is outside its safety bounds")
        if not self.updated_at:
            raise ValueError("migration policy updated_at is required")

    def with_updates(self, **changes: Any) -> MigrationPolicy:
        allowed = {
            field.name
            for field in fields(self)
            if field.name not in {"subject_id", "revision", "updated_at", "state_hash"}
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown migration policy fields: {sorted(unknown)}")
        values = dict(changes)
        for name in ("allowed_target_ids", "allowed_regions"):
            if name in values:
                raw = values[name]
                if isinstance(raw, str) or not isinstance(raw, (list, tuple, set)):
                    raise TypeError(f"{name} must be a sequence of strings")
                values[name] = tuple(dict.fromkeys(str(item).strip() for item in raw))
        if (
            values.get("enabled") is True
            and "approval_mode" not in values
            and self.approval_mode == "disabled"
        ):
            values["approval_mode"] = "manual"
        if values.get("enabled") is False and "approval_mode" not in values:
            values["approval_mode"] = "disabled"
        updated = replace(self, **values, revision=self.revision + 1, updated_at=utc_now())
        updated = replace(updated, state_hash=updated.compute_state_hash())
        updated.validate()
        return updated


_POLICY_FIELDS = frozenset(
    field.name
    for field in fields(MigrationPolicy)
    if field.name not in {"subject_id", "revision", "updated_at", "state_hash"}
)


class MigrationStore:
    """Durable migration policy and append-only audit boundary."""

    def __init__(self, database: Database):
        self.database = database

    def read_policy(self, subject_id: str) -> MigrationPolicy:
        validate_subject_id(subject_id)
        with self.database.transaction() as connection:
            self._ensure_policy(connection, subject_id)
            row = connection.execute(
                "SELECT * FROM migration_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration policy not found: {subject_id}")
            return MigrationPolicy.from_record(row)

    def update_policy(
        self, subject_id: str, expected_revision: int, patch: Mapping[str, Any], actor: str
    ) -> MigrationPolicy:
        validate_subject_id(subject_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected migration policy revision must be positive")
        if not isinstance(patch, Mapping) or not patch or set(patch) - _POLICY_FIELDS:
            raise ValueError("migration policy patch is invalid")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("migration policy actor is invalid")
        with self.database.transaction() as connection:
            self._ensure_policy(connection, subject_id)
            row = connection.execute(
                "SELECT * FROM migration_policies WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            current = MigrationPolicy.from_record(row)
            if current.revision != expected_revision:
                raise MigrationPolicyConflictError(
                    f"migration policy revision changed: expected {expected_revision}, found {current.revision}"
                )
            updated = current.with_updates(**dict(patch))
            result = connection.execute(
                """UPDATE migration_policies SET enabled=?, approval_mode=?, emergency_recovery_enabled=?, local_wallet_transfer_enabled=?, wallet_mode=?, allowed_target_ids_json=?, allowed_regions_json=?, min_free_bytes=?, max_cost_microusd=?, max_downtime_seconds=?, maintenance_window_start_minute=?, maintenance_window_duration_minutes=?, trust_level=?, rejection_cooldown_seconds=?, proposal_expiry_seconds=?, revision=?, updated_at=?, state_hash=? WHERE subject_id=? AND revision=?""",
                (*self._row_values(updated, False), subject_id, current.revision),
            )
            if result.rowcount != 1:
                raise MigrationPolicyConflictError("migration policy changed during update")
            self._append_audit(
                connection,
                subject_id,
                "migration_policy_updated",
                actor.strip(),
                {"revision": updated.revision, "changed_fields": sorted(patch)},
            )
            return updated

    @staticmethod
    def _ensure_policy(connection: Any, subject_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM subject_identity WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(f"subject not found: {subject_id}")
        default = MigrationPolicy.default(subject_id)
        connection.execute(
            """INSERT OR IGNORE INTO migration_policies(subject_id, enabled, approval_mode, emergency_recovery_enabled, local_wallet_transfer_enabled, wallet_mode, allowed_target_ids_json, allowed_regions_json, min_free_bytes, max_cost_microusd, max_downtime_seconds, maintenance_window_start_minute, maintenance_window_duration_minutes, trust_level, rejection_cooldown_seconds, proposal_expiry_seconds, revision, updated_at, state_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            MigrationStore._row_values(default, True),
        )

    @staticmethod
    def _row_values(policy: MigrationPolicy, include_subject: bool) -> tuple[Any, ...]:
        values = (
            int(policy.enabled),
            policy.approval_mode,
            int(policy.emergency_recovery_enabled),
            int(policy.local_wallet_transfer_enabled),
            policy.wallet_mode,
            canonical_json(list(policy.allowed_target_ids)),
            canonical_json(list(policy.allowed_regions)),
            policy.min_free_bytes,
            policy.max_cost_microusd,
            policy.max_downtime_seconds,
            policy.maintenance_window_start_minute,
            policy.maintenance_window_duration_minutes,
            policy.trust_level,
            policy.rejection_cooldown_seconds,
            policy.proposal_expiry_seconds,
            policy.revision,
            policy.updated_at,
            policy.state_hash,
        )
        return (policy.subject_id, *values) if include_subject else values

    @staticmethod
    def _append_audit(
        connection: Any, subject_id: str, action: str, actor: str, payload: Mapping[str, Any]
    ) -> None:
        payload_json = canonical_json(dict(payload))
        occurred_at = utc_now()
        connection.execute(
            "INSERT INTO migration_audit_events(audit_id, subject_id, action, actor, payload_json, occurred_at, state_hash) VALUES (?,?,?,?,?,?,?)",
            (
                new_id("migrationaudit"),
                subject_id,
                action,
                actor,
                payload_json,
                occurred_at,
                content_hash(
                    {
                        "subject_id": subject_id,
                        "action": action,
                        "actor": actor,
                        "payload_json": payload_json,
                        "occurred_at": occurred_at,
                    }
                ),
            ),
        )
