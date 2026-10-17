"""Secret-free planning for encrypted backup artifacts.

The root maintenance command and the management diagnostics use the same
inventory and deletion policy.  Planning never mutates the backup directory;
the caller must perform a separate, locked apply after rechecking hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class BackupRetentionPolicy:
    """Conservative artifact policy with a separately enforced recovery floor."""

    def __init__(
        self,
        keep_verified_count: int = 5,
        max_age_days: int | None = 90,
        max_bytes: int | None = 10_000_000_000,
        unverified_grace_days: int = 7,
        minimum_recovery_points: int = 2,
    ) -> None:
        if not 1 <= keep_verified_count <= 100:
            raise ValueError("deployment_retention_out_of_range")
        if max_age_days is not None and not 1 <= max_age_days <= 36_500:
            raise ValueError("backup_max_age_out_of_range")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("backup_max_bytes_out_of_range")
        if not 1 <= unverified_grace_days <= 36_500:
            raise ValueError("backup_unverified_grace_out_of_range")
        if not 1 <= minimum_recovery_points <= 100:
            raise ValueError("backup_minimum_recovery_points_out_of_range")
        self.keep_verified_count = keep_verified_count
        self.max_age_days = max_age_days
        self.max_bytes = max_bytes
        self.unverified_grace_days = unverified_grace_days
        self.minimum_recovery_points = minimum_recovery_points

    def public(self) -> dict[str, int | None]:
        return {
            "keep_verified_count": self.keep_verified_count,
            "max_age_days": self.max_age_days,
            "max_bytes": self.max_bytes,
            "unverified_grace_days": self.unverified_grace_days,
            "minimum_recovery_points": self.minimum_recovery_points,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> BackupRetentionPolicy:
        if not isinstance(payload, dict):
            raise ValueError("backup retention policy is invalid")
        expected = {
            "keep_verified_count",
            "max_age_days",
            "max_bytes",
            "unverified_grace_days",
            "minimum_recovery_points",
        }
        if set(payload) != expected:
            raise ValueError("backup retention policy fields are invalid")
        for key, value in payload.items():
            if key == "max_age_days" or key == "max_bytes":
                if value is not None and type(value) is not int:
                    raise ValueError("backup retention policy value is invalid")
            elif type(value) is not int:
                raise ValueError("backup retention policy value is invalid")
        return cls(**payload)


def read_marker(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8192:
            return None
        if os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def file_hash(path: Path) -> str:
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise ValueError("unsafe_file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _backup_timestamp(path: Path, marker: dict[str, Any] | None = None) -> datetime:
    value = (marker or {}).get("verified_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def backup_inventory(
    backups: Path,
    *,
    now: datetime | None = None,
    marker_reader: Any = read_marker,
    hash_file: Any = file_hash,
) -> list[dict[str, Any]]:
    """Return a bounded, secret-free inventory of backup artifacts."""

    moment = now or datetime.now(UTC)
    inventory: list[dict[str, Any]] = []
    for path in sorted(backups.glob("noyra-*.noyra-backup")):
        if path.is_symlink() or not path.is_file():
            continue
        marker = marker_reader(path.with_name(path.name + ".verified.json"))
        try:
            digest = hash_file(path)
        except (OSError, ValueError):
            digest = None
        size = path.stat().st_size
        verified = (
            marker is not None
            and digest is not None
            and marker.get("format") == "noyra-verified-upgrade-backup/v1"
            and marker.get("backup") == path.name
            and marker.get("restore_verified") is True
            and marker.get("size", size) == size
            and isinstance(marker.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(marker["sha256"])) is not None
            and marker.get("sha256") == digest
        )
        timestamp = _backup_timestamp(path, marker)
        inventory.append(
            {
                "name": path.name,
                "size": size,
                "sha256": marker.get("sha256") if verified and marker else None,
                "verified": verified,
                "created_at": timestamp.isoformat(),
                "age_days": max(0, int((moment - timestamp).total_seconds() // 86400)),
            }
        )
    return inventory


def _backup_space_status(verified_bytes: int, unverified_bytes: int, max_bytes: int | None) -> str:
    total_bytes = verified_bytes + unverified_bytes
    if max_bytes is None or total_bytes <= max_bytes:
        return "within_limit"
    if unverified_bytes:
        if verified_bytes > max_bytes:
            return "unverified_and_protected_recovery_points_exceed_limit"
        return "unverified_backups_exceed_limit"
    return "protected_recovery_points_exceed_limit"


def backup_gc_plan_id(
    policy: BackupRetentionPolicy,
    inventory: list[dict[str, Any]],
    protected: set[str],
    deletions: list[dict[str, Any]],
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "policy": policy.public(),
                "inventory": inventory,
                "protected": sorted(protected),
                "deletions": deletions,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def backup_gc_plan(
    backups: Path,
    policy: BackupRetentionPolicy,
    *,
    protected_names: set[str] | None = None,
    now: datetime | None = None,
    marker_reader: Any = read_marker,
    hash_file: Any = file_hash,
) -> dict[str, Any]:
    """Plan artifact deletion while preserving a recoverable verified floor."""

    inventory = backup_inventory(
        backups,
        now=now,
        marker_reader=marker_reader,
        hash_file=hash_file,
    )
    protected = set(protected_names or ())
    verified = sorted(
        (item for item in inventory if item["verified"]),
        key=lambda item: (item["created_at"], item["name"]),
        reverse=True,
    )
    required_count = max(policy.keep_verified_count, policy.minimum_recovery_points)
    floor = {item["name"] for item in verified[: policy.minimum_recovery_points]} | protected
    keep = {item["name"] for item in verified[:required_count]} | protected
    deletions = [
        {**item, "reason": "count_exceeded"}
        for item in verified
        if item["name"] not in keep and item["name"] not in protected
    ]
    remaining = [item for item in verified if item["name"] in keep and item["name"] not in floor]
    if policy.max_age_days is not None:
        for item in remaining:
            if item["age_days"] > policy.max_age_days:
                keep.discard(item["name"])
                deletions.append({**item, "reason": "max_age_exceeded"})
        deleted_names = {row["name"] for row in deletions}
        remaining = [item for item in remaining if item["name"] not in deleted_names]
    if policy.max_bytes is not None:
        deleted_names = {row["name"] for row in deletions}
        current_bytes = sum(
            int(item["size"]) for item in verified if item["name"] not in deleted_names
        )
        for item in sorted(remaining, key=lambda row: (row["created_at"], row["name"])):
            if current_bytes <= policy.max_bytes:
                break
            current_bytes -= int(item["size"])
            keep.discard(item["name"])
            deletions.append({**item, "reason": "max_bytes_exceeded"})
    attention = [
        {**item, "reason": "unverified_backup_attention"}
        for item in inventory
        if not item["verified"] and item["age_days"] > policy.unverified_grace_days
    ]
    deleted_names = {row["name"] for row in deletions}
    remaining_verified = [item for item in verified if item["name"] not in deleted_names]
    remaining_verified_bytes = sum(int(item["size"]) for item in remaining_verified)
    unverified_bytes = sum(int(item["size"]) for item in inventory if not item["verified"])
    backup_names = [str(row["name"]) for row in deletions]
    plan = {
        "backups": backup_names,
        "backup_details": deletions,
        "backup_attention": attention,
        "backup_inventory": inventory,
        "backup_protected": sorted(keep | protected),
        "backup_summary": {
            "verified_bytes_after_plan": remaining_verified_bytes,
            "unverified_bytes": unverified_bytes,
            "total_bytes_after_plan": remaining_verified_bytes + unverified_bytes,
            "max_bytes": policy.max_bytes,
            "space_status": _backup_space_status(
                remaining_verified_bytes, unverified_bytes, policy.max_bytes
            ),
            "verified_recovery_points_after_plan": len(remaining_verified),
            "minimum_recovery_points": policy.minimum_recovery_points,
            "recovery_points_status": (
                "satisfied"
                if len(remaining_verified) >= policy.minimum_recovery_points
                else "below_minimum"
            ),
        },
    }
    plan["cursor"] = {
        "version": 1,
        "next_name": backup_names[0] if backup_names else None,
        "pending_names": backup_names,
    }
    plan["plan_id"] = backup_gc_plan_id(policy, inventory, protected, deletions)
    return plan
