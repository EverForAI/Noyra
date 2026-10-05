#!/usr/bin/env python3
"""Root-owned deployment volume preflight and conservative artifact retention."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MARKER = ".noyra-verified-install.json"
PROCESS_REFERENCE_BYTES = 1_048_576


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


def file_hash(path: Path) -> str:
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise ValueError("unsafe_file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
                "restore_verified": True,
            },
        )
    finally:
        shutil.rmtree(scratch)


def gc_plan(
    releases: Path, backups: Path, *, keep: int = 5, proc_root: Path = Path("/proc")
) -> dict[str, list[str]]:
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
    return {
        "releases": [p.name for p in candidates if p.name not in retained],
        "backups": [
            p.name for p in backup_candidates if valid_replacement and p.name not in kept_backups
        ],
    }


def apply_gc(releases: Path, backups: Path, plan: dict[str, list[str]]) -> None:
    # Caller holds the install lock; privileged parent directories exclude other writers.
    for name in plan["releases"]:
        if not SEGMENT.fullmatch(name):
            raise ValueError("invalid_release_name")
        path = releases / name
        if path.is_symlink() or path.resolve().parent != releases.resolve():
            raise ValueError("unsafe_release_path")
        shutil.rmtree(path)
    for name in plan["backups"]:
        if Path(name).name != name or not name.endswith(".noyra-backup"):
            raise ValueError("invalid_backup_name")
        path = backups / name
        if path.is_symlink():
            raise ValueError("unsafe_backup_path")
        path.unlink()
        path.with_name(path.name + ".verified.json").unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "verify-backup", "mark-release", "gc"))
    parser.add_argument("--releases", type=Path, default=Path("/opt/noyra/releases"))
    parser.add_argument("--backups", type=Path, default=Path("/var/backups/noyra"))
    parser.add_argument("--data", type=Path, default=Path("/var/lib/noyra"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--keep", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        parser.error("root_on_linux_required")
    try:
        safe_directory(args.releases)
        if args.operation != "preflight":
            safe_directory(args.backups)
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
                plan = gc_plan(args.releases, args.backups, keep=args.keep)
                if args.apply:
                    apply_gc(args.releases, args.backups, plan)
            finally:
                if opened:
                    os.close(descriptor)
            result = {"status": "applied" if args.apply else "preview", **plan}
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
