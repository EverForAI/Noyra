"""Source-owned, task-scoped authority for exporting local wallet material."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database

from .approval_evidence import audit_payload
from .manager import MigrationManager, MigrationTask
from .policy import MigrationPolicy, MigrationStore
from .targets import TargetRegistry

GRANT = "migration_local_wallet_approved"
REVOKE = "migration_local_wallet_revoked"
FORMAT = "noyra-local-wallet-approval/v1"
_STATES = {"approved", "preparing", "transferring", "restoring", "validating", "cutover"}


def _now() -> datetime:
    return datetime.now(UTC)


def _scope(
    connection: Any, task: MigrationTask, address: str, *, grant: bool = False
) -> dict[str, Any]:
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("migration local wallet address is invalid")
    row = connection.execute(
        "SELECT * FROM migration_tasks WHERE task_id=? AND subject_id=?",
        (task.task_id, task.subject_id),
    ).fetchone()
    if row is None:
        raise ValueError("local wallet migration task unavailable")
    MigrationManager._assert_task_integrity(row)
    if (
        row["status"] not in ({"approved"} if grant else _STATES)
        or any(
            row[key] != getattr(task, key)
            for key in ("proposal_id", "target_id", "policy_revision", "source_epoch")
        )
        or MigrationManager._parse_timestamp(row["expires_at"]) <= _now()
    ):
        raise ValueError("local wallet migration task is stale")
    policy_row = MigrationStore.policy_row(connection, task.subject_id)
    if policy_row is None:
        raise ValueError("local wallet migration policy unavailable")
    policy = MigrationPolicy.from_record(policy_row)
    if (
        not policy.enabled
        or not policy.local_wallet_transfer_enabled
        or policy.wallet_mode != "local_wallet_transfer"
        or policy.revision != task.policy_revision
    ):
        raise ValueError("local wallet migration policy is stale")
    target = connection.execute(
        "SELECT * FROM migration_targets WHERE subject_id=? AND target_id=?",
        (task.subject_id, task.target_id),
    ).fetchone()
    if target is None:
        raise ValueError("local wallet migration target unavailable")
    TargetRegistry._assert_row_integrity(target)
    if target["status"] != "active" or target["recipient_key_fingerprint"] is None:
        raise ValueError("local wallet migration recipient key unavailable")
    return {
        "format": FORMAT,
        "task_id": task.task_id,
        "subject_id": task.subject_id,
        "proposal_id": task.proposal_id,
        "policy_revision": policy.revision,
        "source_epoch": task.source_epoch,
        "target_id": task.target_id,
        "target_generation": int(target["enrollment_generation"]),
        "target_key_fingerprint": target["key_fingerprint"],
        "recipient_key_fingerprint": target["recipient_key_fingerprint"],
        "address": address.casefold(),
    }


def _latest(connection: Any, task: MigrationTask) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM migration_audit_events WHERE subject_id=? AND action IN (?,?) "
        "AND json_extract(payload_json,'$.task_id')=? ORDER BY rowid DESC LIMIT 1",
        (task.subject_id, GRANT, REVOKE, task.task_id),
    ).fetchone()
    if row is None:
        return None
    payload = audit_payload(row)
    if row["action"] == REVOKE:
        raise ValueError("local wallet approval was revoked")
    return payload


def _validate(
    payload: dict[str, Any] | None, scope: dict[str, Any], task: MigrationTask
) -> dict[str, str]:
    if payload is None or set(payload) != {*scope, "approval"}:
        raise ValueError("source local wallet approval is required")
    if any(payload[key] != value for key, value in scope.items()):
        raise ValueError("source local wallet approval scope changed")
    approval = payload["approval"]
    if (
        not isinstance(approval, dict)
        or set(approval) != {"approval_id", "task_id", "address", "channel_id", "expires_at"}
        or not isinstance(approval["approval_id"], str)
        or not re.fullmatch(r"[0-9a-f]{48}", approval["approval_id"])
        or approval["task_id"] != task.task_id
        or str(approval["address"]).casefold() != scope["address"]
        or approval["channel_id"] != "recipient-" + scope["recipient_key_fingerprint"]
    ):
        raise ValueError("source local wallet approval binding is invalid")
    expiry = MigrationManager._parse_timestamp(approval["expires_at"])
    if expiry <= _now() or expiry > MigrationManager._parse_timestamp(task.expires_at):
        raise ValueError("source local wallet approval has expired")
    return dict(approval)


def grant_local_wallet(
    database: Database, task: MigrationTask, address: str, *, actor: str
) -> dict[str, str]:
    MigrationManager._validate_actor(actor)
    with database.transaction() as connection:
        scope = _scope(connection, task, address, grant=True)
        try:
            latest = _latest(connection, task)
        except ValueError as error:
            if str(error) != "local wallet approval was revoked":
                raise
            latest = None
        if latest is not None:
            # A retry returns the same live permit. Stale/expired grants need
            # an explicit new grant; the request itself is that confirmation.
            try:
                return _validate(latest, scope, task)
            except ValueError:
                pass
        approval = {
            "approval_id": secrets.token_hex(24),
            "task_id": task.task_id,
            "address": address,
            "channel_id": "recipient-" + scope["recipient_key_fingerprint"],
            "expires_at": min(
                _now() + timedelta(hours=1), MigrationManager._parse_timestamp(task.expires_at)
            ).isoformat(),
        }
        MigrationStore._append_audit(
            connection, task.subject_id, GRANT, actor, scope | {"approval": approval}
        )
        return approval


def load_local_wallet(database: Database, task: MigrationTask, address: str) -> dict[str, str]:
    with database.read_transaction() as connection:
        return _validate(_latest(connection, task), _scope(connection, task, address), task)


def assert_local_wallet_binding(
    database: Database, task: MigrationTask, binding: Mapping[str, Any]
) -> None:
    if binding.get("mode") != "local_wallet_transfer":
        return
    approval = load_local_wallet(database, task, str(binding.get("address", "")))
    if binding.get("approval") != approval:
        raise ValueError("local wallet binding differs from source approval")


def revoke_local_wallet(
    database: Database, task: MigrationTask, approval_id: str, *, actor: str
) -> None:
    MigrationManager._validate_actor(actor)
    with database.transaction() as connection:
        latest = connection.execute(
            "SELECT * FROM migration_audit_events WHERE subject_id=? AND action IN (?,?) "
            "AND json_extract(payload_json,'$.task_id')=? ORDER BY rowid DESC LIMIT 1",
            (task.subject_id, GRANT, REVOKE, task.task_id),
        ).fetchone()
        if latest is not None and latest["action"] == REVOKE:
            revoked = audit_payload(latest)
            if revoked.get("approval_id") != approval_id:
                raise ValueError("local wallet approval changed before revocation")
            return
        payload = _latest(connection, task)
        if payload is None or payload.get("approval", {}).get("approval_id") != approval_id:
            raise ValueError("local wallet approval changed before revocation")
        MigrationStore._append_audit(
            connection,
            task.subject_id,
            REVOKE,
            actor,
            {"task_id": task.task_id, "approval_id": approval_id},
        )


def authorize_material(
    database: Database,
    task: MigrationTask,
    binding: Mapping[str, Any],
    artifact_id: str,
    *,
    recipient_key_fingerprint: str,
) -> None:
    with database.transaction() as connection:
        scope = _scope(connection, task, str(binding.get("address", "")))
        approval = _validate(_latest(connection, task), scope, task)
        if recipient_key_fingerprint != scope["recipient_key_fingerprint"]:
            raise ValueError("wallet artifact recipient differs from approved target")
        if binding.get("approval") != approval:
            raise ValueError("local wallet binding differs from source approval")
        rows = connection.execute(
            "SELECT * FROM migration_audit_events WHERE subject_id=? "
            "AND action='migration_local_wallet_material_authorized' "
            "AND json_extract(payload_json,'$.approval_id')=?",
            (task.subject_id, approval["approval_id"]),
        ).fetchall()
        for row in rows:
            value = audit_payload(row)
            if value != {
                "task_id": task.task_id,
                "approval_id": approval["approval_id"],
                "artifact_id": artifact_id,
            }:
                raise ValueError("local wallet approval already bound to another artifact")
        if not rows:
            MigrationStore._append_audit(
                connection,
                task.subject_id,
                "migration_local_wallet_material_authorized",
                "runtime",
                {
                    "task_id": task.task_id,
                    "approval_id": approval["approval_id"],
                    "artifact_id": artifact_id,
                },
            )
