"""Bounded, authenticated file inventory for a complete subject transfer.

Control-plane credentials, machine identity and provider secrets are rebound
on the target. They are never implicitly copied with subject files.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from noyra.core.database import Database
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import canonical_json, content_hash
from noyra.world.observation_archive import ObservationContentArchive

PAYLOAD_FORMAT = "noyra-subject-state/v1"
STATE_ROOTS = ("subject", "workspace", "training_raw", "exports")
INVENTORY_NAME = "subject-state.json"
MAX_FILES = 100_000
MAX_BYTES = 10 * 1024**3
MAX_INVENTORY_BYTES = 32 * 1024**2


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def open_regular(path: Path) -> BinaryIO:
    for ancestor in (path, *path.parents):
        details = ancestor.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise ValueError("subject payload links are forbidden")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    details = os.fstat(descriptor)
    path_details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or (details.st_dev, details.st_ino) != (path_details.st_dev, path_details.st_ino)
    ):
        os.close(descriptor)
        raise ValueError("subject payload must contain ordinary private files")
    return os.fdopen(descriptor, "rb")


def _name(name: Any) -> str:
    if (
        not isinstance(name, str)
        or len(name) > 1024
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise ValueError("subject payload path is invalid")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or str(path) != name
        or any(part in {".", ".."} or part.endswith((".", " ")) for part in path.parts)
        or any(
            part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(10)),
                *(f"LPT{i}" for i in range(10)),
            }
            for part in path.parts
        )
        or (
            name != "noyra.sqlite3"
            and path.parts[0] not in STATE_ROOTS
            and name
            not in {
                "migration-wallet",
                "migration-wallet/wallet.json",
                "migration-wallet/password",
                "migration-wallet/rpc.json",
            }
        )
    ):
        raise ValueError("subject payload path is outside the permitted roots")
    return name


def stage_payload(source: Path, destination: Path, subject_id: str, expected_digest: str) -> None:
    """Copy into a root-owned staging tree and recheck the authenticated inventory."""
    inventory = verify_payload(source, subject_id)
    if hashlib.sha256(canonical_json(inventory).encode()).hexdigest() != expected_digest:
        raise ValueError("subject payload inventory binding mismatch")
    for name in inventory["directories"]:
        (destination / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in inventory["files"]:
        target = destination / entry["path"]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open_regular(source / entry["path"]) as stream, target.open("xb") as out:
            shutil.copyfileobj(stream, out, 1024 * 1024)
        os.chmod(target, 0o600)
    (destination / INVENTORY_NAME).write_text(canonical_json(inventory), encoding="utf-8")
    os.chmod(destination / INVENTORY_NAME, 0o600)
    if os.name == "nt" and (destination / "migration-wallet").exists():
        from noyra.core.at_rest import _harden_tree

        _harden_tree(destination / "migration-wallet")
    verify_payload(destination, subject_id)
    verify_archives(destination, subject_id)


def create_payload(
    database: Path,
    data_root: Path,
    destination: Path,
    subject_id: str,
    *,
    local_wallet: tuple[Path, Path, Path] | None = None,
) -> dict[str, Any]:
    """The caller holds the source fence; preserve the complete SQLite schema."""
    entries: list[dict[str, Any]] = []
    tree_directories: list[str] = []
    sources = {"noyra.sqlite3": database}
    if local_wallet is not None:
        sources.update(
            {
                "migration-wallet/wallet.json": local_wallet[0],
                "migration-wallet/password": local_wallet[1],
                "migration-wallet/rpc.json": local_wallet[2],
            }
        )
        tree_directories.append("migration-wallet")
    for root_name in STATE_ROOTS:
        root = data_root / root_name
        if root.is_symlink() or (
            root.exists() and getattr(root.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("subject payload root is a link")
        if not root.exists():
            continue
        for folder, directories, files in os.walk(root, followlinks=False):
            tree_directories.append(_name(Path(folder).relative_to(data_root).as_posix()))
            if len(tree_directories) > MAX_FILES:
                raise ValueError("subject payload directory limit exceeded")
            for name in directories:
                child = Path(folder) / name
                details = child.lstat()
                if child.is_symlink() or getattr(details, "st_file_attributes", 0) & 0x400:
                    raise ValueError("subject payload directory is a link")
            for name in files:
                child = Path(folder) / name
                sources[_name(child.relative_to(data_root).as_posix())] = child
                if len(sources) > MAX_FILES:
                    raise ValueError("subject payload file limit exceeded")
    total = 0
    folded = {name.casefold() for name in tree_directories}
    if len(folded) != len(tree_directories):
        raise ValueError("subject payload paths collide")
    for name, path in sorted(sources.items()):
        if name.casefold() in folded:
            raise ValueError("subject payload paths collide")
        folded.add(name.casefold())
        digest = file_digest(path)
        size = path.stat().st_size
        total += size
        if total > MAX_BYTES:
            raise ValueError("subject payload byte limit exceeded")
        entries.append({"path": name, "bytes": size, "sha256": digest})
    inventory = {
        "format": PAYLOAD_FORMAT,
        "subject_id": subject_id,
        "files": entries,
        "directories": sorted(tree_directories),
        "total_bytes": total,
    }
    raw = canonical_json(inventory).encode()
    if len(raw) > MAX_INVENTORY_BYTES:
        raise ValueError("subject payload inventory too large")
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with (
        os.fdopen(descriptor, "wb") as output,
        tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        info = tarfile.TarInfo(INVENTORY_NAME)
        info.size, info.mode = len(raw), 0o600
        archive.addfile(info, io.BytesIO(raw))
        for entry in entries:
            info = tarfile.TarInfo(entry["path"])
            info.size, info.mode = entry["bytes"], 0o600
            with open_regular(sources[entry["path"]]) as source:
                archive.addfile(info, source)
            if file_digest(sources[entry["path"]]) != entry["sha256"]:
                raise ValueError("subject files changed during snapshot")
    os.chmod(destination, 0o600)
    return inventory


def _inventory(raw: bytes, subject_id: str) -> dict[str, Any]:
    if len(raw) > MAX_INVENTORY_BYTES:
        raise ValueError("subject payload inventory too large")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "subject_id", "files", "directories", "total_bytes"}
        or value["format"] != PAYLOAD_FORMAT
        or value["subject_id"] != subject_id
        or not isinstance(value["files"], list)
        or len(value["files"]) > MAX_FILES
        or not isinstance(value["directories"], list)
        or len(value["directories"]) > MAX_FILES
    ):
        raise ValueError("subject payload inventory invalid")
    names: set[str] = set()
    for directory in value["directories"]:
        name = _name(directory)
        if name == "noyra.sqlite3" or name.casefold() in names:
            raise ValueError("subject payload directory invalid")
        names.add(name.casefold())
    total = 0
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise ValueError("subject payload entry invalid")
        name = _name(entry["path"])
        if name.casefold() in names or type(entry["bytes"]) is not int or entry["bytes"] < 0:
            raise ValueError("subject payload entry invalid")
        names.add(name.casefold())
        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("subject payload digest invalid")
        total += entry["bytes"]
    if total > MAX_BYTES or total != value["total_bytes"] or "noyra.sqlite3" not in names:
        raise ValueError("subject payload size or database missing")
    return value


def restore_payload(source: Path, destination: Path, subject_id: str) -> Path:
    if destination.exists():
        raise ValueError("subject payload destination already exists")
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(source, "r|") as archive:
            first = archive.next()
            if (
                first is None
                or first.name != INVENTORY_NAME
                or not first.isfile()
                or first.size > MAX_INVENTORY_BYTES
            ):
                raise ValueError("subject payload inventory missing")
            stream = archive.extractfile(first)
            assert stream is not None
            raw = stream.read(MAX_INVENTORY_BYTES + 1)
            inventory = _inventory(raw, subject_id)
            for name in inventory["directories"]:
                (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            expected = {entry["path"]: entry for entry in inventory["files"]}
            for info in archive:
                if info.name == INVENTORY_NAME and info is first:
                    continue
                entry = expected.pop(info.name, None)
                if entry is None or not info.isfile() or info.size != entry["bytes"]:
                    raise ValueError("subject payload file set mismatch")
                target = destination / _name(info.name)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source_stream = archive.extractfile(info)
                assert source_stream is not None
                with target.open("xb") as out:
                    shutil.copyfileobj(source_stream, out, 1024 * 1024)
                os.chmod(target, 0o600)
                if file_digest(target) != entry["sha256"]:
                    raise ValueError("subject payload content mismatch")
            if expected:
                raise ValueError("subject payload file missing")
        (destination / INVENTORY_NAME).write_bytes(raw)
        os.chmod(destination / INVENTORY_NAME, 0o600)
        if os.name == "nt" and (destination / "migration-wallet").exists():
            from noyra.core.at_rest import _harden_tree

            _harden_tree(destination / "migration-wallet")
        verify_payload(destination, subject_id)
        return destination / "noyra.sqlite3"
    except Exception:
        shutil.rmtree(destination)
        raise


def verify_payload(root: Path, subject_id: str) -> dict[str, Any]:
    with open_regular(root / INVENTORY_NAME) as stream:
        inventory = _inventory(stream.read(MAX_INVENTORY_BYTES + 1), subject_id)
    expected = {entry["path"] for entry in inventory["files"]}
    actual: set[str] = set()
    directories: set[str] = set()
    for folder, children, files in os.walk(root, followlinks=False):
        for name in children:
            child = Path(folder) / name
            if child.is_symlink() or getattr(child.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("subject payload directory is a link")
            directories.add(child.relative_to(root).as_posix())
        for name in files:
            relative = (Path(folder) / name).relative_to(root).as_posix()
            if relative != INVENTORY_NAME:
                actual.add(relative)
        if len(actual) > MAX_FILES or len(directories) > MAX_FILES:
            raise ValueError("subject payload file limit exceeded")
    if actual != expected or directories != set(inventory["directories"]):
        raise ValueError("subject payload restored file set mismatch")
    for entry in inventory["files"]:
        path = root / entry["path"]
        if path.stat().st_size != entry["bytes"] or file_digest(path) != entry["sha256"]:
            raise ValueError("subject payload restored content mismatch")
    with closing(
        sqlite3.connect(f"{(root / 'noyra.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        if (
            connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("subject payload database integrity failed")
    return inventory


def verify_archives(root: Path, subject_id: str) -> None:
    database = Database(root / "noyra.sqlite3", initialize=False, read_only=True)
    result = EventPayloadArchive.verify_subject_integrity(
        database, root / "subject" / "cold", subject_id
    )
    if result.status != "ok":
        raise ValueError("subject event archives unavailable")
    with database.connection() as connection:
        archived = connection.execute(
            "SELECT 1 FROM observations WHERE subject_id=? "
            "AND content_archive_key IS NOT NULL LIMIT 1",
            (subject_id,),
        ).fetchone()
    if archived:
        ObservationContentArchive(
            database,
            root / "subject" / "cold",
            subject_id=subject_id,
            create_root=False,
            cache_cloud_restores=False,
        ).verify_integrity(subject_id)


def verify_runtime_credentials(database_path: Path, secret_root: Path, subject_id: str) -> None:
    """Require real provisioned target secrets before activating preserved references.

    Secret bytes never travel in the subject payload. Inactive historical
    references remain readable without requiring revoked credentials.
    """
    database = Database(database_path, initialize=False, read_only=True)
    contracts = (
        ("search_provider_configs", "search", "key_reference", "status='active'"),
        ("cognitive_resource_keys", "models", "key_reference", "status!='revoked'"),
        ("embedding_resources", "embedding", "config_id || '.key'", "status='active'"),
    )
    with database.connection() as connection:
        for table, folder, reference_column, predicate in contracts:
            rows = connection.execute(
                f"SELECT {reference_column} AS reference,key_fingerprint FROM {table} "
                f"WHERE subject_id=? AND {predicate}",
                (subject_id,),
            )
            for row in rows:
                reference = row["reference"]
                if (
                    not isinstance(reference, str)
                    or PurePosixPath(reference).name != reference
                    or any(c in reference for c in ("\\", ":"))
                ):
                    raise ValueError("target credential reference is invalid")
                with open_regular(secret_root / folder / reference) as stream:
                    raw = stream.read(16 * 1024 + 1)
                if (
                    len(raw) > 16 * 1024
                    or content_hash({"api_key": raw.decode("utf-8")}) != row["key_fingerprint"]
                ):
                    raise ValueError("target runtime credential is unavailable or mismatched")
        transports = list(
            connection.execute(
                "SELECT transport_id,secret_reference FROM interaction_transports "
                "WHERE subject_id=? AND status='active' AND secret_reference IS NOT NULL",
                (subject_id,),
            )
        )
    if transports:
        from noyra.interaction.transport import TransportStore

        store = TransportStore(database, secret_root / "transports", repair_on_init=False)
        for row in transports:
            if row["secret_reference"] != f"{row['transport_id']}.json":
                raise ValueError("target transport credential reference is invalid")
            with open_regular(secret_root / "transports" / row["secret_reference"]) as stream:
                if len(stream.read(64 * 1024 + 1)) > 64 * 1024:
                    raise ValueError("target transport credential is too large")
            store.secret(row["transport_id"], subject_id=subject_id)
