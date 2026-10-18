#!/usr/bin/env python3
"""Root-owned deployment volume preflight and conservative artifact retention."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from noyra.core.backup_retention import (
    BackupRetentionPolicy,
    _backup_space_status,
)
from noyra.core.backup_retention import backup_gc_plan as _backup_gc_plan
from noyra.core.backup_retention import backup_gc_plan_id as _backup_gc_plan_id
from noyra.core.backup_retention import backup_inventory as _backup_inventory
from noyra.core.backup_retention import file_hash as _file_hash
from noyra.core.backup_retention import read_marker as _read_marker
from noyra.core.storage_mode import detect_storage_layout
from noyra.core.types import content_hash

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MARKER = ".noyra-verified-install.json"
PROCESS_REFERENCE_BYTES = 1_048_576
MAX_RESUMABLE_GC_ACTIONS = 1_000

read_marker = _read_marker
file_hash = _file_hash


def backup_inventory(backups: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    return _backup_inventory(backups, now=now, marker_reader=read_marker, hash_file=file_hash)


def backup_gc_plan(
    backups: Path,
    policy: BackupRetentionPolicy,
    *,
    protected_names: set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _backup_gc_plan(
        backups,
        policy,
        protected_names=protected_names,
        now=now,
        marker_reader=read_marker,
        hash_file=file_hash,
    )


def _database_subject_and_backup_policy(
    database_path: Path, fallback: BackupRetentionPolicy
) -> tuple[str | None, BackupRetentionPolicy]:
    """Read the persisted policy without making the root CLI depend on service code."""
    if not database_path.is_file() or database_path.is_symlink():
        return None, fallback
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            subject = connection.execute(
                "SELECT subject_id FROM subject_identity ORDER BY subject_id LIMIT 1"
            ).fetchone()
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='backup_retention_policies'"
            ).fetchone()
            if subject is None or table is None:
                return None if subject is None else str(subject["subject_id"]), fallback
            row = connection.execute(
                "SELECT * FROM backup_retention_policies WHERE subject_id=?",
                (subject["subject_id"],),
            ).fetchone()
            if row is None:
                return str(subject["subject_id"]), fallback
            state = dict(row)
            state_hash = state.pop("state_hash")
            if content_hash(state) != state_hash:
                raise ValueError("backup_retention_policy_integrity")
            return str(subject["subject_id"]), BackupRetentionPolicy.from_payload(
                json.loads(row["settings_json"])
            )
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("backup_retention_policy_unavailable") from error


def _record_backup_gc_run(
    database_path: Path,
    subject_id: str | None,
    plan: dict[str, Any],
    *,
    status: str,
    deleted_count: int = 0,
    failure_code: str | None = None,
    required: bool = False,
    cursor_override: dict[str, Any] | None = None,
) -> str | None:
    if subject_id is None or not database_path.is_file() or database_path.is_symlink():
        if required:
            raise RuntimeError("backup_retention_audit_unavailable")
        return None
    values = {
        "run_id": f"backupretention_{os.urandom(16).hex()}",
        "subject_id": subject_id,
        "plan_id": str(plan["plan_id"]),
        "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "completed_at": (
            None if status == "planned" else datetime.now(UTC).isoformat(timespec="milliseconds")
        ),
        "status": status,
        "cursor_json": json.dumps(
            cursor_override if cursor_override is not None else plan.get("cursor", {}),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "deleted_count": deleted_count,
        "failure_code": failure_code,
    }
    state_hash = content_hash(values)
    try:
        with sqlite3.connect(database_path) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_retention_runs'"
            ).fetchone()
            if table is None:
                if required:
                    raise RuntimeError("backup_retention_audit_unavailable")
                return None
            connection.execute(
                "INSERT INTO backup_retention_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*values.values(), state_hash),
            )
    except sqlite3.DatabaseError as error:
        if required:
            raise RuntimeError("backup_retention_audit_unavailable") from error
        return None
    return str(values["run_id"])


def _backup_gc_run_row_state(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    state = dict(zip(columns, row, strict=True))
    state_hash = state.pop("state_hash", None)
    if not isinstance(state_hash, str) or content_hash(state) != state_hash:
        raise ValueError("backup_retention_run_integrity")
    return state


def _load_pending_backup_gc_run(
    database_path: Path, subject_id: str
) -> tuple[str, str, dict[str, Any], int] | None:
    try:
        with sqlite3.connect(database_path) as connection:
            cursor = connection.execute(
                "SELECT * FROM backup_retention_runs WHERE subject_id=? AND status='planned' "
                "ORDER BY started_at DESC",
                (subject_id,),
            )
            columns = [str(column[0]) for column in cursor.description or ()]
            rows = cursor.fetchall()
    except sqlite3.DatabaseError as error:
        raise ValueError("backup_retention_audit_unavailable") from error
    resumable: list[tuple[str, str, dict[str, Any], int]] = []
    for row in rows:
        state = _backup_gc_run_row_state(row, columns)
        try:
            payload = json.loads(str(state["cursor_json"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("backup_retention_cursor_invalid") from error
        if isinstance(payload, dict) and payload.get("version") == 2:
            next_index = payload.get("next_index")
            actions = payload.get("actions")
            if (
                type(next_index) is not int
                or next_index < 0
                or not isinstance(actions, list)
                or next_index > len(actions)
            ):
                raise ValueError("backup_retention_cursor_invalid")
            resumable.append(
                (
                    str(state["run_id"]),
                    str(state["plan_id"]),
                    payload,
                    int(state["deleted_count"]),
                )
            )
    if len(resumable) > 1:
        raise ValueError("backup_retention_multiple_pending_runs")
    return resumable[0] if resumable else None


def _update_backup_gc_run(
    database_path: Path,
    run_id: str,
    *,
    status: str,
    cursor: dict[str, Any],
    deleted_count: int,
    failure_code: str | None = None,
    required: bool = True,
) -> None:
    if status not in {"planned", "applied", "failed"} or deleted_count < 0:
        raise ValueError("backup_retention_run_state_invalid")
    try:
        with sqlite3.connect(database_path) as connection:
            cursor_result = connection.execute(
                "SELECT * FROM backup_retention_runs WHERE run_id=?", (run_id,)
            )
            columns = [str(column[0]) for column in cursor_result.description or ()]
            row = cursor_result.fetchone()
            if row is None:
                raise ValueError("backup_retention_run_missing")
            state = _backup_gc_run_row_state(row, columns)
            now = datetime.now(UTC).isoformat(timespec="milliseconds")
            state["completed_at"] = None if status == "planned" else now
            state["status"] = status
            state["cursor_json"] = json.dumps(cursor, sort_keys=True, separators=(",", ":"))
            state["deleted_count"] = deleted_count
            state["failure_code"] = failure_code
            state["state_hash"] = content_hash(state)
            connection.execute(
                "UPDATE backup_retention_runs SET completed_at=?,status=?,cursor_json=?,"
                "deleted_count=?,failure_code=?,state_hash=? WHERE run_id=?",
                (
                    state["completed_at"],
                    status,
                    state["cursor_json"],
                    deleted_count,
                    failure_code,
                    state["state_hash"],
                    run_id,
                ),
            )
    except (OSError, sqlite3.DatabaseError) as error:
        if required:
            raise RuntimeError("backup_retention_audit_unavailable") from error


def safe_directory(path: Path, *, owned: bool = True) -> Path:
    if not path.is_absolute():
        raise ValueError("absolute_directory_required")
    for parent in (*reversed(path.parents), path):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
            raise ValueError("unsafe_directory")
        if owned and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            raise ValueError("unsafe_directory_owner")
    return path


def tree_size(path: Path) -> int:
    size = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            child = Path(root) / name
            metadata = child.lstat()
            if child.is_symlink():
                continue
            if stat.S_ISREG(metadata.st_mode):
                size += metadata.st_size
    return size


def preflight(
    releases: Path, backups: Path, data: Path, *, reserve: int = 500_000_000
) -> dict[str, Any]:
    """Budget separately for each device, including temporary backup/restore copies."""
    volumes: dict[int, tuple[Path, int]] = {}
    source_bytes = tree_size(data)
    for path, required in (
        (releases, 2_000_000_000),
        (backups, max(source_bytes * 2, 64_000_000)),
        (data, max(source_bytes * 6, 64_000_000)),
    ):
        probe = path
        while not probe.exists():
            probe = probe.parent
        device = probe.stat().st_dev
        prior = volumes.get(device, (probe, 0))
        volumes[device] = (probe, prior[1] + required)
    checks = []
    for path, required in volumes.values():
        free = shutil.disk_usage(path).free
        if free < required + reserve:
            raise ValueError("deployment_disk_headroom_insufficient")
        checks.append({"path": str(path), "required_bytes": required + reserve, "free_bytes": free})
    return {"status": "ok", "volumes": checks}


def atomic_marker(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("verification_marker_already_exists")
    descriptor, temporary = tempfile.mkstemp(prefix=".verify-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verified_release(path: Path) -> bool:
    marker = read_marker(path / MARKER) or {}
    return (
        marker.get("format") == "noyra-verified-install/v1" and marker.get("readiness") == "passed"
    )


def verified_backup(path: Path) -> bool:
    marker = read_marker(path.with_name(path.name + ".verified.json")) or {}
    return (
        marker.get("format") == "noyra-verified-upgrade-backup/v1"
        and marker.get("backup") == path.name
        and marker.get("restore_verified") is True
        and isinstance(marker.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", marker["sha256"]) is not None
    )


def process_reference(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(PROCESS_REFERENCE_BYTES + 1)
    if len(raw) > PROCESS_REFERENCE_BYTES:
        raise ValueError("process_reference_oversized")
    return raw.decode("utf-8", errors="replace")


def verify_backup(backup: Path, data: Path, keyring: Path) -> None:
    from noyra.core.at_rest import EncryptedBackupManager

    digest = file_hash(backup)
    scratch = Path(tempfile.mkdtemp(prefix=".upgrade-verify-", dir=data))
    try:
        manager = EncryptedBackupManager(data, keyring)
        manager.restore(backup, scratch / "restored")
        atomic_marker(
            backup.with_name(backup.name + ".verified.json"),
            {
                "format": "noyra-verified-upgrade-backup/v1",
                "sha256": digest,
                "backup": backup.name,
                "size": backup.stat().st_size,
                "verified_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "restore_verified": True,
            },
        )
    finally:
        shutil.rmtree(scratch)


def gc_plan(
    releases: Path,
    backups: Path,
    *,
    keep: int = 5,
    proc_root: Path = Path("/proc"),
    backup_policy: BackupRetentionPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if keep < 3 or keep > 100:
        raise ValueError("deployment_retention_out_of_range")
    protected: set[str] = set()
    for name in ("current", "previous"):
        pointer = releases.parent / name
        if not pointer.is_symlink():
            if pointer.exists():
                raise ValueError("unsafe_release_pointer")
            continue
        target = pointer.resolve(strict=True)
        if target.parent != releases.resolve() or not SEGMENT.fullmatch(target.name):
            raise ValueError("unsafe_release_pointer")
        protected.add(target.name)
    candidates = sorted(
        (
            p
            for p in releases.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and SEGMENT.fullmatch(p.name)
            and verified_release(p)
        ),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    retained = {p.name for p in candidates[:keep]} | protected
    # Never remove a release still referenced by a running process.
    if proc_root.is_dir():
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            for member in ("exe", "cwd"):
                try:
                    path = (process / member).resolve(strict=True)
                    relative = path.relative_to(releases.resolve())
                    retained.add(relative.parts[0])
                except (OSError, ValueError):
                    pass
            try:
                mapped = process_reference(process / "maps")
                command = process_reference(process / "cmdline")
                for candidate in candidates:
                    if str(candidate) in mapped or str(candidate) in command:
                        retained.add(candidate.name)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # If process references cannot be inspected, defer release GC.
                retained.update(p.name for p in candidates)
    backup_candidates = sorted(
        (
            p
            for p in backups.glob("noyra-*.noyra-backup")
            if not p.is_symlink() and p.is_file() and verified_backup(p)
        ),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    # A verified, unchanged replacement is required before deleting older backups.
    valid_replacement = bool(backup_candidates) and (
        read_marker(backup_candidates[0].with_name(backup_candidates[0].name + ".verified.json"))
        or {}
    ).get("sha256") == file_hash(backup_candidates[0])
    kept_backups = {p.name for p in backup_candidates[:keep]}
    for name in retained:
        marker = read_marker(releases / name / MARKER) or {}
        if isinstance(marker.get("backup"), str):
            kept_backups.add(marker["backup"])
    policy = backup_policy or BackupRetentionPolicy(keep_verified_count=keep)
    artifact_plan = backup_gc_plan(
        backups,
        policy,
        protected_names=kept_backups,
        now=now,
    )
    # Keep the historical replacement guard: a tampered newest backup must
    # never turn into a deletion plan even when a size/age policy is present.
    if not valid_replacement:
        artifact_plan["backups"] = []
        artifact_plan["backup_details"] = []
        artifact_plan["cursor"] = {
            "version": 1,
            "next_name": None,
            "pending_names": [],
        }
        inventory = artifact_plan["backup_inventory"]
        verified_bytes = sum(int(item["size"]) for item in inventory if item["verified"])
        unverified_bytes = sum(int(item["size"]) for item in inventory if not item["verified"])
        summary = artifact_plan["backup_summary"]
        summary["verified_bytes_after_plan"] = verified_bytes
        summary["total_bytes_after_plan"] = verified_bytes + unverified_bytes
        summary["verified_recovery_points_after_plan"] = sum(
            1 for item in inventory if item["verified"]
        )
        summary["recovery_points_status"] = (
            "satisfied"
            if summary["verified_recovery_points_after_plan"] >= summary["minimum_recovery_points"]
            else "below_minimum"
        )
        summary["space_status"] = _backup_space_status(
            verified_bytes,
            unverified_bytes,
            summary["max_bytes"],
        )
        artifact_plan["backup_gc_blocked_reason"] = "verified_replacement_unavailable"
        artifact_plan["plan_id"] = _backup_gc_plan_id(
            policy,
            inventory,
            set(artifact_plan["backup_protected"]),
            [],
        )
    return {
        "releases": [p.name for p in candidates if p.name not in retained],
        **artifact_plan,
    }


def _gc_actions(releases: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        {
            "kind": "release",
            "name": name,
            "marker_sha256": file_hash(releases / name / MARKER),
        }
        for name in plan["releases"]
    ]
    actions.extend(
        {
            "kind": "backup",
            "name": item["name"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        for item in plan["backup_details"]
    )
    return actions


def _action_exists(releases: Path, backups: Path, action: dict[str, Any]) -> bool:
    name = action.get("name")
    kind = action.get("kind")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("backup_retention_cursor_invalid")
    if kind == "release":
        path = releases / name
        if path.is_symlink():
            raise ValueError("unsafe_release_path")
        return path.exists()
    if kind == "backup":
        path = backups / name
        marker_path = path.with_name(path.name + ".verified.json")
        if path.is_symlink() or marker_path.is_symlink():
            raise ValueError("unsafe_backup_path")
        if path.exists():
            return True
        if marker_path.exists():
            marker = read_marker(marker_path)
            if (
                marker is None
                or marker.get("backup") != name
                or marker.get("sha256") != action.get("sha256")
            ):
                raise ValueError("unsafe_backup_path")
            marker_path.unlink()
        return False
    raise ValueError("backup_retention_cursor_invalid")


def _require_action_still_planned(
    action: dict[str, Any], releases: Path, plan: dict[str, Any]
) -> None:
    name = action.get("name")
    if action.get("kind") == "release":
        if name not in plan["releases"]:
            raise ValueError("backup_retention_resume_plan_changed")
        marker_path = releases / str(name) / MARKER
        if marker_path.is_symlink() or file_hash(marker_path) != action.get("marker_sha256"):
            raise ValueError("backup_retention_resume_plan_changed")
        return
    if action.get("kind") == "backup":
        current = {
            item["name"]: item for item in plan["backup_details"] if isinstance(item, dict)
        }.get(name)
        if (
            current is None
            or current.get("sha256") != action.get("sha256")
            or current.get("size") != action.get("size")
        ):
            raise ValueError("backup_retention_resume_plan_changed")
        return
    raise ValueError("backup_retention_cursor_invalid")


def apply_gc(
    releases: Path,
    backups: Path,
    plan: dict[str, Any],
    *,
    on_applied: Any = None,
) -> None:
    # Caller holds the install lock; privileged parent directories exclude other writers.
    release_paths: list[Path] = []
    release_names = plan["releases"]
    release_details = {
        item.get("name"): item for item in plan.get("release_details", []) if isinstance(item, dict)
    }
    if len(set(release_names)) != len(release_names):
        raise ValueError("duplicate_release_name")
    for name in release_names:
        if not SEGMENT.fullmatch(name):
            raise ValueError("invalid_release_name")
        path = releases / name
        if (
            path.is_symlink()
            or path.resolve().parent != releases.resolve()
            or not verified_release(path)
        ):
            raise ValueError("unsafe_release_path")
        if release_details:
            detail = release_details.get(name)
            if detail is None or file_hash(path / MARKER) != detail.get("marker_sha256"):
                raise ValueError("unsafe_release_path")
        release_paths.append(path)

    backup_paths: list[tuple[Path, Path]] = []
    backup_names = plan["backups"]
    if len(set(backup_names)) != len(backup_names):
        raise ValueError("duplicate_backup_name")
    details = {
        item.get("name"): item for item in plan.get("backup_details", []) if isinstance(item, dict)
    }
    if len(details) != len(plan.get("backup_details", [])) or set(details) != set(backup_names):
        raise ValueError("backup_plan_details_mismatch")
    for name in backup_names:
        if Path(name).name != name or not name.endswith(".noyra-backup"):
            raise ValueError("invalid_backup_name")
        path = backups / name
        marker_path = path.with_name(path.name + ".verified.json")
        detail = details.get(name)
        marker = read_marker(marker_path)
        if (
            path.is_symlink()
            or detail is None
            or not verified_backup(path)
            or marker is None
            or marker.get("sha256") != detail.get("sha256")
            or marker.get("sha256") != file_hash(path)
            or path.stat().st_size != detail.get("size")
            or marker_path.is_symlink()
        ):
            raise ValueError("unsafe_backup_path")
        backup_paths.append((path, marker_path))

    for path in release_paths:
        shutil.rmtree(path)
        if on_applied is not None:
            on_applied("release", path.name)
    for path, marker_path in backup_paths:
        path.unlink()
        marker_path.unlink()
        if on_applied is not None:
            on_applied("backup", path.name)


def apply_resumable_gc(
    database_path: Path,
    subject_id: str,
    releases: Path,
    backups: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    pending = _load_pending_backup_gc_run(database_path, subject_id)
    resumed = pending is not None
    if pending is None:
        actions = _gc_actions(releases, plan)
        if len(actions) > MAX_RESUMABLE_GC_ACTIONS:
            raise ValueError("backup_retention_plan_too_large")
        cursor: dict[str, Any] = {"version": 2, "actions": actions, "next_index": 0}
        run_id = _record_backup_gc_run(
            database_path,
            subject_id,
            plan,
            status="planned",
            required=True,
            cursor_override=cursor,
        )
        if run_id is None:
            raise RuntimeError("backup_retention_audit_unavailable")
        plan_id = str(plan["plan_id"])
        deleted_count = 0
    else:
        run_id, plan_id, cursor, deleted_count = pending
        actions = cursor["actions"]
        if len(actions) > MAX_RESUMABLE_GC_ACTIONS:
            raise ValueError("backup_retention_cursor_invalid")

    next_index = int(cursor["next_index"])
    try:
        for index in range(next_index, len(actions)):
            action = actions[index]
            if not isinstance(action, dict):
                raise ValueError("backup_retention_cursor_invalid")
            if not _action_exists(releases, backups, action):
                next_index = index + 1
                deleted_count += 1
                cursor["next_index"] = next_index
                _update_backup_gc_run(
                    database_path,
                    run_id,
                    status="planned",
                    cursor=cursor,
                    deleted_count=deleted_count,
                )
                continue

            _require_action_still_planned(action, releases, plan)
            name = str(action["name"])
            if action["kind"] == "release":
                action_plan: dict[str, Any] = {
                    "releases": [name],
                    "release_details": [{"name": name, "marker_sha256": action["marker_sha256"]}],
                    "backups": [],
                    "backup_details": [],
                }
            else:
                detail = next(
                    item
                    for item in plan["backup_details"]
                    if item["name"] == name and item["sha256"] == action["sha256"]
                )
                action_plan = {
                    "releases": [],
                    "backups": [name],
                    "backup_details": [detail],
                }

            def record_progress(
                kind: str,
                applied_name: str,
                expected_kind: str = action["kind"],
                expected_name: str = name,
                next_index_value: int = index + 1,
            ) -> None:
                nonlocal deleted_count, next_index
                if kind != expected_kind or applied_name != expected_name:
                    raise ValueError("backup_retention_progress_mismatch")
                next_index = next_index_value
                deleted_count += 1
                cursor["next_index"] = next_index
                _update_backup_gc_run(
                    database_path,
                    run_id,
                    status="planned",
                    cursor=cursor,
                    deleted_count=deleted_count,
                )

            apply_gc(releases, backups, action_plan, on_applied=record_progress)

        cursor["next_index"] = len(actions)
        _update_backup_gc_run(
            database_path,
            run_id,
            status="applied",
            cursor=cursor,
            deleted_count=deleted_count,
            required=True,
        )
    except Exception as error:
        try:
            _update_backup_gc_run(
                database_path,
                run_id,
                status="failed",
                cursor=cursor,
                deleted_count=deleted_count,
                failure_code=type(error).__name__,
                required=True,
            )
        except Exception as audit_error:
            raise RuntimeError("backup_retention_terminal_audit_unavailable") from audit_error
        raise
    return {"plan_id": plan_id, "resumed": resumed, "deleted_count": deleted_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "verify-backup", "mark-release", "gc"))
    parser.add_argument("--releases", type=Path, default=Path("/opt/noyra/releases"))
    parser.add_argument("--backups", type=Path, default=Path("/var/backups/noyra"))
    parser.add_argument("--data", type=Path, default=Path("/var/lib/noyra"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--keep", type=int, default=5)
    parser.add_argument("--keep-verified-count", type=int)
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--max-bytes", type=int, default=10_000_000_000)
    parser.add_argument("--unverified-grace-days", type=int, default=7)
    parser.add_argument("--minimum-recovery-points", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        parser.error("root_on_linux_required")
    try:
        safe_directory(args.releases)
        if args.operation != "preflight":
            safe_directory(args.backups)
        if args.operation in {"preflight", "verify-backup", "gc"}:
            layout = detect_storage_layout(args.data, args.backups)
            if not layout.backup_ready:
                raise ValueError(layout.backup_reason_code or "backup_storage_unavailable")
        if args.operation == "preflight":
            result = preflight(args.releases, args.backups, args.data)
        elif args.operation == "verify-backup":
            if args.backup is None or args.backup.parent != args.backups:
                raise ValueError("backup_outside_boundary")
            verify_backup(args.backup, args.data, Path("/etc/noyra/backup-keyring.json"))
            result = {"status": "backup_restore_verified"}
        elif args.operation == "mark-release":
            if not args.release or not SEGMENT.fullmatch(args.release):
                raise ValueError("invalid_release")
            safe_directory(args.releases / args.release)
            atomic_marker(
                args.releases / args.release / MARKER,
                {
                    "format": "noyra-verified-install/v1",
                    "readiness": "passed",
                    "backup": args.backup.name if args.backup else None,
                },
            )
            result = {"status": "release_verified"}
        else:
            lock_module: Any = import_module("fcntl")

            lock_path = args.releases.parent / ".install.lock"
            descriptor = args.lock_fd
            opened = descriptor is None
            if opened:
                descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            assert descriptor is not None
            try:
                metadata = lock_path.lstat()
                held = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) != (held.st_dev, held.st_ino):
                    raise ValueError("install_lock_mismatch")
                lock_module.flock(descriptor, lock_module.LOCK_EX | lock_module.LOCK_NB)
                fallback_policy = BackupRetentionPolicy(
                    keep_verified_count=(
                        args.keep if args.keep_verified_count is None else args.keep_verified_count
                    ),
                    max_age_days=args.max_age_days,
                    max_bytes=args.max_bytes,
                    unverified_grace_days=args.unverified_grace_days,
                    minimum_recovery_points=args.minimum_recovery_points,
                )
                database_path = args.database or (args.data / "noyra.sqlite3")
                subject_id, policy = _database_subject_and_backup_policy(
                    database_path, fallback_policy
                )
                plan = gc_plan(
                    args.releases,
                    args.backups,
                    keep=args.keep,
                    backup_policy=policy,
                )
                if args.apply:
                    if subject_id is None:
                        raise RuntimeError("backup_retention_audit_unavailable")
                    execution = apply_resumable_gc(
                        database_path,
                        subject_id,
                        args.releases,
                        args.backups,
                        plan,
                    )
                else:
                    execution = None
            finally:
                if opened:
                    os.close(descriptor)
            result = {
                "status": "applied" if args.apply else "preview",
                "execution": execution,
                **plan,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_type": type(error).__name__,
                    "error_code": "deployment_maintenance_failed",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
