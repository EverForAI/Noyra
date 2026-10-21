#!/usr/bin/env python3
"""Root-owned deployment volume preflight and conservative artifact retention."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
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
from noyra.core.maintenance_evidence import (
    EVIDENCE_DIRECTORY,
    validate_evidence_directory,
    validate_evidence_file,
)
from noyra.core.storage_mode import detect_storage_layout
from noyra.core.types import content_hash

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MARKER = ".noyra-verified-install.json"
PROCESS_REFERENCE_BYTES = 1_048_576
# A plan may contain any number of actions.  This is the maximum number
# consumed by one invocation so SQLite locks and directory scans stay bounded.
MAX_RESUMABLE_GC_ACTIONS = 1_000
GC_MANIFEST_VERSION = 1

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


@contextmanager
def _gc_database_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Create SQLite journals as the database owner, never as the root helper.

    The CLI is single-threaded. Restoring euid before any filesystem deletion
    keeps all privileged action authority outside this small database scope.
    """
    original_uid = original_gid = None
    if os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0:
        import threading

        metadata = database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("backup_retention_database_unsafe")
        if metadata.st_uid != 0:
            if threading.active_count() != 1:
                raise ValueError("backup_retention_privilege_scope_requires_single_thread")
            original_uid, original_gid = os.geteuid(), os.getegid()
    try:
        if original_uid is not None:
            os.setegid(metadata.st_gid)
            os.seteuid(metadata.st_uid)
        with closing(sqlite3.connect(database_path)) as connection, connection:
            yield connection
    finally:
        if original_uid is not None:
            os.seteuid(original_uid)
            os.setegid(original_gid)


def _database_subject_and_backup_policy(
    database_path: Path, fallback: BackupRetentionPolicy
) -> tuple[str | None, BackupRetentionPolicy]:
    """Read the persisted policy without making the root CLI depend on service code."""
    if not database_path.is_file() or database_path.is_symlink():
        return None, fallback
    try:
        with _gc_database_connection(database_path) as connection:
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
    run_id: str | None = None,
) -> str | None:
    if subject_id is None or not database_path.is_file() or database_path.is_symlink():
        if required:
            raise RuntimeError("backup_retention_audit_unavailable")
        return None
    values = {
        "run_id": run_id or f"backupretention_{os.urandom(16).hex()}",
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
        with _gc_database_connection(database_path) as connection:
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
        with _gc_database_connection(database_path) as connection:
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
        if isinstance(payload, dict) and payload.get("version") in {2, 3, 4, 5}:
            next_index = payload.get("next_index")
            actions = payload.get("actions")
            action_count = payload.get(
                "action_count", len(actions) if isinstance(actions, list) else None
            )
            valid_actions = (
                isinstance(actions, list)
                if payload.get("version") in {2, 3}
                else (
                    type(action_count) is int
                    and action_count >= 0
                    and isinstance(payload.get("manifest_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["manifest_sha256"]) is not None
                )
            )
            if (
                type(next_index) is not int
                or next_index < 0
                or not valid_actions
                or next_index > int(action_count)
                or (
                    payload.get("version") == 5
                    and (
                        not isinstance(payload.get("next_sha256"), str)
                        or re.fullmatch(r"[0-9a-f]{64}", payload["next_sha256"]) is None
                    )
                )
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
        with _gc_database_connection(database_path) as connection:
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
    result = {
        "releases": [p.name for p in candidates if p.name not in retained],
        **artifact_plan,
    }
    # The resumable cursor covers releases and backups together.  The core
    # backup plan id alone would not notice a changed release set.
    result["legacy_backup_plan_id"] = result["plan_id"]
    result["plan_id"] = content_hash(
        {
            "backup_plan_id": result["plan_id"],
            "releases": result["releases"],
            "release_details": result.get("release_details", []),
            "protected": result.get("backup_protected", []),
        }
    )
    return result


def _gc_actions(releases: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_iter_gc_actions(releases, plan))


def _iter_gc_actions(releases: Path, plan: dict[str, Any]):
    for name in plan["releases"]:
        yield {
            "kind": "release",
            "name": name,
            "marker_sha256": file_hash(releases / name / MARKER),
        }
    for item in plan["backup_details"]:
        yield {
            "kind": "backup",
            "name": item["name"],
            "size": item["size"],
            "sha256": item["sha256"],
        }


def _expected_evidence_owner() -> int:
    return 0


def _gc_manifest_path(database_path: Path, run_id: str) -> Path:
    if not SEGMENT.fullmatch(database_path.name) or not re.fullmatch(
        r"[A-Za-z0-9_]{1,128}", run_id
    ):
        raise ValueError("backup_retention_evidence_identity_invalid")
    return (
        database_path.parent
        / EVIDENCE_DIRECTORY
        / (f".{database_path.name}.backup-retention-{run_id}.manifest")
    )


def _gc_index_path(database_path: Path, run_id: str) -> Path:
    return _gc_manifest_path(database_path, run_id).with_suffix(".index")


def _ensure_gc_evidence(database_path: Path) -> None:
    data = database_path.parent
    root = data / EVIDENCE_DIRECTORY
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o750)
        if os.name == "posix":
            os.chown(root, _expected_evidence_owner(), data.stat().st_gid)
            root.chmod(0o750)
        _fsync_directory(data)
    validate_evidence_directory(data, owner=_expected_evidence_owner())
    # Relocate only the old, strictly validated root artifacts. Never chmod or
    # adopt arbitrary files under the service-owned root.
    for legacy in data.glob(f".{database_path.name}.backup-retention-*.manifest"):
        validate_evidence_file(legacy, data.stat().st_dev, owner=_expected_evidence_owner())
        destination = root / legacy.name
        if destination.exists() or destination.is_symlink():
            raise ValueError("backup_retention_evidence_migration_conflict")
        os.replace(legacy, destination)
        _fsync_directory(root)
        _fsync_directory(data)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_orphan_gc_manifests(database_path: Path) -> None:
    """Remove true orphans and applied manifests; failed plans remain evidence."""

    _ensure_gc_evidence(database_path)
    pattern = f".{database_path.name}.backup-retention-*"
    removable: list[Path] = []
    try:
        with _gc_database_connection(database_path) as connection:
            cursor = connection.execute("SELECT * FROM backup_retention_runs LIMIT 0")
            columns = [str(column[0]) for column in cursor.description or ()]
            for candidate in (database_path.parent / EVIDENCE_DIRECTORY).glob(pattern):
                run_id = (
                    candidate.name.removeprefix(f".{database_path.name}.backup-retention-")
                    .removesuffix(".manifest")
                    .removesuffix(".index")
                )
                try:
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or (
                        os.name == "posix"
                        and (
                            metadata.st_uid != _expected_evidence_owner()
                            or metadata.st_mode & 0o077
                        )
                    )
                ):
                    raise ValueError("backup_retention_orphan_manifest_unsafe")
                row = connection.execute(
                    "SELECT * FROM backup_retention_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    removable.append(candidate)
                    continue
                state = _backup_gc_run_row_state(row, columns)
                status = state.get("status")
                if status in {"planned", "failed"}:
                    continue
                if status != "applied":
                    raise ValueError("backup_retention_run_state_invalid")
                removable.append(candidate)
    except sqlite3.DatabaseError as error:
        raise ValueError("backup_retention_audit_unavailable") from error

    removed = False
    for candidate in removable:
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        removed = True
    if removed:
        _fsync_directory(database_path.parent / EVIDENCE_DIRECTORY)


def _write_gc_manifest(database_path: Path, run_id: str, actions: Any) -> dict[str, Any]:
    """Persist the immutable action list outside the bounded SQLite cursor."""

    _ensure_gc_evidence(database_path)
    path = _gc_manifest_path(database_path, run_id)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    digest = hashlib.sha256()
    bytes_written = 0
    action_count = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for action in actions:
                line = (json.dumps(action, sort_keys=True, separators=(",", ":")) + "\n").encode()
                digest.update(line)
                bytes_written += len(line)
                action_count += 1
                stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "manifest_sha256": digest.hexdigest(),
        "manifest_bytes": bytes_written,
        "action_count": action_count,
    }


def _write_gc_index(database_path: Path, run_id: str, actions: Any) -> dict[str, Any]:
    """An immutable ordinal index with a hash chain anchored in the audit row.

    Creation is linear; each later invocation reads at most one action batch.
    The next hash is committed together with the deletion cursor after each
    action, so resumption does not rescan or trust an unchecked byte offset.
    """
    _ensure_gc_evidence(database_path)
    destination = _gc_index_path(database_path, run_id)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    count = 0
    following = content_hash({"gc_index_end": True})
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute(
                "CREATE TABLE actions (ordinal INTEGER PRIMARY KEY, "
                "payload TEXT NOT NULL, following TEXT NOT NULL)"
            )
            for count, action in enumerate(actions, 1):
                payload = json.dumps(action, sort_keys=True, separators=(",", ":"))
                if len(payload.encode("utf-8")) > 8192:
                    raise ValueError("backup_retention_action_too_large")
                connection.execute("INSERT INTO actions VALUES(?,?,?)", (count - 1, payload, ""))
            reader = connection.execute("SELECT ordinal,payload FROM actions ORDER BY ordinal DESC")
            while rows := reader.fetchmany(MAX_RESUMABLE_GC_ACTIONS):
                for ordinal, payload in rows:
                    connection.execute(
                        "UPDATE actions SET following=? WHERE ordinal=?", (following, ordinal)
                    )
                    following = content_hash(
                        {"ordinal": ordinal, "payload": payload, "following": following}
                    )
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "action_count": count,
        "manifest_sha256": following,
        "next_sha256": following,
        "manifest_bytes": destination.stat().st_size,
    }


def _read_gc_index_batch(
    database_path: Path, run_id: str, cursor: dict[str, Any], end: int
) -> list[tuple[dict[str, Any], str]]:
    source = _gc_index_path(database_path, run_id)
    validate_evidence_file(
        source, database_path.parent.stat().st_dev, owner=_expected_evidence_owner()
    )
    start = int(cursor["next_index"])
    expected = cursor.get("next_sha256")
    result: list[tuple[dict[str, Any], str]] = []
    try:
        with closing(
            sqlite3.connect(source.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as connection:
            rows = connection.execute(
                "SELECT ordinal,payload,following FROM actions WHERE ordinal>=? AND ordinal<? "
                "AND length(payload)<=8192 ORDER BY ordinal",
                (start, end),
            ).fetchall()
        if len(rows) != end - start:
            raise ValueError("backup_retention_manifest_integrity")
        for index, (ordinal, payload, following) in enumerate(rows, start):
            if (
                ordinal != index
                or content_hash({"ordinal": ordinal, "payload": payload, "following": following})
                != expected
            ):
                raise ValueError("backup_retention_manifest_integrity")
            action = json.loads(payload)
            if not isinstance(action, dict):
                raise ValueError("backup_retention_manifest_invalid")
            result.append((action, following))
            expected = following
        if end == cursor["action_count"] and expected != content_hash({"gc_index_end": True}):
            raise ValueError("backup_retention_manifest_integrity")
    except sqlite3.DatabaseError as error:
        raise ValueError("backup_retention_manifest_integrity") from error
    return result


def _read_gc_manifest_batch(
    database_path: Path,
    run_id: str,
    cursor: dict[str, Any],
    *,
    start_index: int,
    end_index: int,
) -> list[dict[str, Any]]:
    path = _gc_manifest_path(database_path, run_id)
    if path.is_symlink() or not path.is_file():
        raise ValueError("backup_retention_manifest_missing")
    digest = hashlib.sha256()
    batch: list[dict[str, Any]] = []
    action_count = 0
    try:
        with path.open("rb") as stream:
            for raw in stream:
                digest.update(raw)
                try:
                    action = json.loads(raw)
                except (UnicodeDecodeError, ValueError) as error:
                    raise ValueError("backup_retention_manifest_invalid") from error
                if not isinstance(action, dict):
                    raise ValueError("backup_retention_manifest_invalid")
                if start_index <= action_count < end_index:
                    batch.append(action)
                action_count += 1
    except OSError as error:
        raise ValueError("backup_retention_manifest_unavailable") from error
    expected_batch_size = max(0, min(end_index, action_count) - start_index)
    if (
        digest.hexdigest() != cursor.get("manifest_sha256")
        or action_count != cursor.get("action_count")
        or len(batch) != expected_batch_size
    ):
        raise ValueError("backup_retention_manifest_integrity")
    return batch


def _gc_manifest_matches_current_plan(
    database_path: Path,
    run_id: str,
    cursor: dict[str, Any],
    releases: Path,
    backups: Path,
    plan: dict[str, Any],
) -> bool:
    release_names = set(plan.get("releases", []))
    backup_details = {
        item.get("name"): item for item in plan.get("backup_details", []) if isinstance(item, dict)
    }
    manifest_path = _gc_manifest_path(database_path, run_id)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    digest = hashlib.sha256()
    count = 0
    manifest_keys: set[tuple[object, object]] = set()
    try:
        with manifest_path.open("rb") as stream:
            for raw in stream:
                digest.update(raw)
                count += 1
                action = json.loads(raw)
                if not isinstance(action, dict):
                    return False
                kind, name = action.get("kind"), action.get("name")
                key = (kind, name)
                if key in manifest_keys:
                    return False
                manifest_keys.add(key)
                if kind == "release" and name in release_names:
                    marker = releases / name / MARKER
                    if marker.is_file() and not marker.is_symlink():
                        if file_hash(marker) != action.get("marker_sha256"):
                            return False
                    elif _action_state(releases, backups, action) == "present":
                        return False
                elif kind == "backup" and name in backup_details:
                    detail = backup_details[name]
                    if detail.get("sha256") != action.get("sha256") or detail.get(
                        "size"
                    ) != action.get("size"):
                        return False
                    if _action_state(releases, backups, action) == "present":
                        continue
                elif _action_state(releases, backups, action) == "present":
                    return False
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    current_keys = {(item["kind"], item["name"]) for item in _iter_gc_actions(releases, plan)}
    return (
        digest.hexdigest() == cursor.get("manifest_sha256")
        and count == cursor.get("action_count")
        and current_keys.issubset(manifest_keys)
    )


def _plan_policy_digest(plan: dict[str, Any]) -> str:
    return content_hash(plan.get("retention_policy", {}))


def _plan_protected_digest(plan: dict[str, Any]) -> str:
    return content_hash(plan.get("backup_protected", []))


def _resume_actions_compatible(
    persisted: list[dict[str, Any]], current: list[dict[str, Any]], releases: Path, backups: Path
) -> bool:
    """Allow only idempotent disappearance of already-planned actions."""

    current_by_key = {(item.get("kind"), item.get("name")): item for item in current}
    persisted_by_key = {(item.get("kind"), item.get("name")): item for item in persisted}
    if len(current_by_key) != len(current) or len(persisted_by_key) != len(persisted):
        return False
    if any(key not in persisted_by_key for key in current_by_key):
        return False
    for key, action in persisted_by_key.items():
        current_action = current_by_key.get(key)
        if current_action is not None:
            if current_action != action:
                return False
            continue
        if _action_exists(releases, backups, action):
            return False
    return True


def _current_gc_actions_for_resume(
    releases: Path, backups: Path, plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Rebuild current actions while tolerating already-completed deletes."""

    current: list[dict[str, Any]] = []
    for name in plan.get("releases", []):
        marker = releases / str(name) / MARKER
        if marker.is_file() and not marker.is_symlink():
            current.append(
                {
                    "kind": "release",
                    "name": str(name),
                    "marker_sha256": file_hash(marker),
                }
            )
    details = {item.get("name"): item for item in plan.get("backup_details", [])}
    for name in plan.get("backups", []):
        path = backups / str(name)
        if not path.is_file() or path.is_symlink():
            continue
        detail = details.get(name)
        if isinstance(detail, dict):
            current.append(
                {
                    "kind": "backup",
                    "name": str(name),
                    "size": detail.get("size"),
                    "sha256": detail.get("sha256"),
                }
            )
    return current


def _action_exists(releases: Path, backups: Path, action: dict[str, Any]) -> bool:
    state = _action_state(releases, backups, action)
    return state == "present"


def _action_state(releases: Path, backups: Path, action: dict[str, Any]) -> str:
    """Inspect an action without changing any filesystem state."""

    name = action.get("name")
    kind = action.get("kind")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("backup_retention_cursor_invalid")
    if kind == "release":
        path = releases / name
        if path.is_symlink():
            raise ValueError("unsafe_release_path")
        return "present" if path.exists() else "absent"
    if kind == "backup":
        path = backups / name
        marker_path = path.with_name(path.name + ".verified.json")
        if path.is_symlink() or marker_path.is_symlink():
            raise ValueError("unsafe_backup_path")
        if path.exists():
            return "present"
        if marker_path.exists():
            marker = read_marker(marker_path)
            if (
                marker is None
                or marker.get("format") != "noyra-verified-upgrade-backup/v1"
                or marker.get("backup") != name
                or marker.get("sha256") != action.get("sha256")
                or marker.get("restore_verified") is not True
                or marker.get("size", action.get("size")) != action.get("size")
            ):
                raise ValueError("unsafe_backup_path")
            return "marker_only"
        return "absent"
    raise ValueError("backup_retention_cursor_invalid")


def _cleanup_absent_action_artifacts(releases: Path, backups: Path, action: dict[str, Any]) -> None:
    """Remove only artifacts whose action completion is already durable."""

    if action.get("kind") != "backup":
        return
    name = action.get("name")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("backup_retention_cursor_invalid")
    marker_path = backups / f"{name}.verified.json"
    if marker_path.is_symlink() or not marker_path.exists():
        return
    marker = read_marker(marker_path)
    if (
        marker is not None
        and marker.get("format") == "noyra-verified-upgrade-backup/v1"
        and marker.get("backup") == name
        and marker.get("sha256") == action.get("sha256")
        and marker.get("restore_verified") is True
        and marker.get("size", action.get("size")) == action.get("size")
    ):
        marker_path.unlink()


def _cleanup_completed_manifest_artifacts(
    database_path: Path,
    run_id: str,
    cursor: dict[str, Any],
    releases: Path,
    backups: Path,
    completed_count: int,
) -> None:
    path = _gc_manifest_path(database_path, run_id)
    if path.is_symlink() or not path.is_file():
        raise ValueError("backup_retention_manifest_missing")
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as stream:
            for raw in stream:
                digest.update(raw)
                action = json.loads(raw)
                if not isinstance(action, dict):
                    raise ValueError("backup_retention_manifest_invalid")
                if (
                    count < completed_count
                    and _action_state(releases, backups, action) == "marker_only"
                ):
                    _cleanup_absent_action_artifacts(releases, backups, action)
                count += 1
    except OSError as error:
        raise ValueError("backup_retention_manifest_unavailable") from error
    if count != cursor.get("action_count") or digest.hexdigest() != cursor.get("manifest_sha256"):
        raise ValueError("backup_retention_manifest_integrity")


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
        by_name = plan.get("backup_details_by_name")
        if by_name is None:
            by_name = {
                item["name"]: item for item in plan["backup_details"] if isinstance(item, dict)
            }
        current = by_name.get(name)
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
    database_path: Path, subject_id: str, releases: Path, backups: Path, plan: dict[str, Any]
) -> dict[str, Any]:
    _cleanup_orphan_gc_manifests(database_path)
    pending = _load_pending_backup_gc_run(database_path, subject_id)
    resumed = pending is not None
    superseded_run_id = None
    if pending is not None:
        run_id, plan_id, cursor, deleted_count = pending
        if cursor.get("plan_id", plan_id) != plan_id:
            raise ValueError("backup_retention_cursor_invalid")
        changed = cursor.get("version") != 2 and (
            cursor.get("policy_digest") != _plan_policy_digest(plan)
            or cursor.get("protected_digest") != _plan_protected_digest(plan)
        )
        source = (
            _gc_index_path(database_path, run_id)
            if cursor.get("version") == 5
            else _gc_manifest_path(database_path, run_id)
        )
        missing = (
            cursor.get("version") in {4, 5} and not source.exists() and not source.is_symlink()
        )
        if changed or missing:
            _update_backup_gc_run(
                database_path,
                run_id,
                status="failed",
                cursor=cursor,
                deleted_count=deleted_count,
                failure_code="plan_superseded" if changed else "machine_local_evidence_unavailable",
                required=True,
            )
            superseded_run_id = run_id
            pending = None
            resumed = False
        elif cursor.get("version") != 5:
            # One-time, fully verified conversion of legacy manifests. No old
            # cursor is discarded until the indexed replacement is durable.
            if cursor.get("version") == 4:
                actions = _read_gc_manifest_batch(
                    database_path,
                    run_id,
                    cursor,
                    start_index=0,
                    end_index=int(cursor["action_count"]),
                )
            else:
                actions = cursor["actions"]
            current_actions = _current_gc_actions_for_resume(releases, backups, plan)
            if not _resume_actions_compatible(actions, current_actions, releases, backups):
                _update_backup_gc_run(
                    database_path,
                    run_id,
                    status="failed",
                    cursor=cursor,
                    deleted_count=deleted_count,
                    failure_code="plan_superseded",
                    required=True,
                )
                result = apply_resumable_gc(database_path, subject_id, releases, backups, plan)
                result["superseded_run_id"] = run_id
                return result
            _cleanup_completed_legacy_actions(
                actions[: int(cursor["next_index"])], releases, backups
            )
            manifest = _write_gc_index(database_path, run_id, actions)
            next_index = int(cursor["next_index"])
            cursor = {
                **cursor,
                **manifest,
                "version": 5,
                "next_index": 0,
                "policy_digest": _plan_policy_digest(plan),
                "protected_digest": _plan_protected_digest(plan),
            }
            if next_index:
                # Only migration reads the completed prefix once.
                entries = _read_gc_index_batch(database_path, run_id, cursor, next_index)
                cursor["next_sha256"] = entries[-1][1]
                cursor["next_index"] = next_index
            cursor.pop("actions", None)
            _update_backup_gc_run(
                database_path,
                run_id,
                status="planned",
                cursor=cursor,
                deleted_count=deleted_count,
                required=True,
            )

    if pending is None:
        plan_id = str(plan["plan_id"])
        run_id = f"backupretention_{os.urandom(16).hex()}"
        manifest = _write_gc_index(database_path, run_id, _iter_gc_actions(releases, plan))
        cursor = {
            "version": 5,
            "next_index": 0,
            "plan_id": plan_id,
            "policy_digest": _plan_policy_digest(plan),
            "protected_digest": _plan_protected_digest(plan),
            **manifest,
        }
        try:
            recorded = _record_backup_gc_run(
                database_path,
                subject_id,
                plan,
                status="planned",
                required=True,
                cursor_override=cursor,
                run_id=run_id,
            )
            if recorded != run_id:
                raise RuntimeError("backup_retention_audit_unavailable")
        except Exception:
            _gc_index_path(database_path, run_id).unlink(missing_ok=True)
            raise
        deleted_count = 0

    action_count = int(cursor["action_count"])
    next_index = int(cursor["next_index"])
    release_names = set(plan["releases"])
    backup_details = {item["name"]: item for item in plan["backup_details"]}
    # Build these indexes once per invocation, not once per deleted action.
    indexed_plan = {**plan, "releases": release_names, "backup_details_by_name": backup_details}
    warning = False
    terminalized = False
    try:
        cleanup_action = cursor.pop("cleanup_action", None)
        if (
            cleanup_action is not None
            and _action_state(releases, backups, cleanup_action) == "marker_only"
        ):
            _cleanup_absent_action_artifacts(releases, backups, cleanup_action)
        batch_end = min(action_count, next_index + MAX_RESUMABLE_GC_ACTIONS)
        entries = _read_gc_index_batch(database_path, run_id, cursor, batch_end)
        # Revalidate this whole bounded batch before making any deletion. A
        # protected/changed action terminates the old plan with its evidence.
        for action, _ in entries:
            if _action_state(releases, backups, action) == "present":
                try:
                    _require_action_still_planned(action, releases, indexed_plan)
                except ValueError as error:
                    if str(error) != "backup_retention_resume_plan_changed":
                        raise
                    _update_backup_gc_run(
                        database_path,
                        run_id,
                        status="failed",
                        cursor=cursor,
                        deleted_count=deleted_count,
                        failure_code="plan_superseded",
                        required=True,
                    )
                    terminalized = True
                    result = apply_resumable_gc(database_path, subject_id, releases, backups, plan)
                    result["superseded_run_id"] = run_id
                    return result
        for index, (action, following) in enumerate(entries, start=next_index):

            def record_progress(
                kind: str,
                applied_name: str,
                expected_action: dict[str, Any] = action,
                expected_next: int = index + 1,
                expected_hash: str = following,
            ) -> None:
                nonlocal deleted_count, next_index
                if kind != expected_action["kind"] or applied_name != expected_action["name"]:
                    raise ValueError("backup_retention_progress_mismatch")
                next_index = expected_next
                deleted_count += 1
                cursor.update(
                    next_index=next_index, next_sha256=expected_hash, cleanup_action=expected_action
                )
                _update_backup_gc_run(
                    database_path,
                    run_id,
                    status="planned",
                    cursor=cursor,
                    deleted_count=deleted_count,
                    required=True,
                )

            if _action_state(releases, backups, action) != "present":
                record_progress(action["kind"], action["name"])
                _cleanup_absent_action_artifacts(releases, backups, action)
            else:
                _require_action_still_planned(action, releases, indexed_plan)
                name = action["name"]
                action_plan = {
                    "releases": [name] if action["kind"] == "release" else [],
                    "release_details": [
                        {"name": name, "marker_sha256": action.get("marker_sha256")}
                    ]
                    if action["kind"] == "release"
                    else [],
                    "backups": [name] if action["kind"] == "backup" else [],
                    "backup_details": [backup_details[name]] if action["kind"] == "backup" else [],
                }
                apply_gc(releases, backups, action_plan, on_applied=record_progress)
            cursor.pop("cleanup_action", None)
        status = "planned" if next_index < action_count else "applied"
        _update_backup_gc_run(
            database_path,
            run_id,
            status=status,
            cursor=cursor,
            deleted_count=deleted_count,
            required=True,
        )
        if status == "applied":
            try:
                _gc_index_path(database_path, run_id).unlink(missing_ok=True)
                _gc_manifest_path(database_path, run_id).unlink(missing_ok=True)
                _fsync_directory(database_path.parent / EVIDENCE_DIRECTORY)
            except OSError:
                warning = True
    except Exception as error:
        if terminalized:
            raise
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
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "resumed": resumed,
        "deleted_count": deleted_count,
        "remaining": action_count - next_index,
        "status": "partial" if next_index < action_count else "applied",
        "manifest_cleanup_warning": warning,
        "superseded_run_id": superseded_run_id,
    }


def _cleanup_completed_legacy_actions(
    actions: list[dict[str, Any]], releases: Path, backups: Path
) -> None:
    for action in actions:
        if _action_state(releases, backups, action) == "marker_only":
            _cleanup_absent_action_artifacts(releases, backups, action)


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
                "status": (
                    execution.get("status", "applied")
                    if args.apply and isinstance(execution, dict)
                    else "preview"
                ),
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
