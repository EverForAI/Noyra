"""Provider-neutral archive interface for cold subject data.

The runtime never hands provider credentials to a model.  Providers receive
bytes and object keys only; failures are reported to the caller and can be
queued for a later retry.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import ipaddress
import os
import queue
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .admission import current_commit_scope
from .at_rest import AtRestError, validate_keyring_path
from .database import Database
from .errors import (
    ArchiveAuthenticationError,
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from .identity import IdentityStore, validate_subject_id
from .subject_storage import SubjectStorageDirectory
from .types import canonical_json, content_hash, new_id, strict_int, strict_json_loads, utc_now

ARCHIVE_KEYRING_FORMAT = "noyra-archive-keyring/v1"
MAX_ARCHIVE_KEYRING_BYTES = 1_000_000
MAX_ARCHIVE_KEYS = 64
ARCHIVE_EXPIRED_LEASE_RECOVERY_BUDGET = 1_000
ARCHIVE_ORPHAN_REFERENCE_BUDGET = 100_000
_ARCHIVE_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class ArchiveKeyMaterial:
    key_id: str
    key: bytes
    fingerprint: str
    status: str


class ArchiveKeyring:
    """Versioned external archive keys; SQLite records metadata, never key bytes."""

    def __init__(
        self,
        *,
        generation: int,
        active_key_id: str,
        legacy_key_id: str | None,
        keys: tuple[ArchiveKeyMaterial, ...],
    ):
        if type(generation) is not int or generation < 1:
            raise ValueError("archive keyring generation is invalid")
        if not keys or len(keys) > MAX_ARCHIVE_KEYS:
            raise ValueError("archive keyring key count is invalid")
        for item in keys:
            self._validate_key_id(item.key_id)
            if (
                not isinstance(item.key, bytes)
                or len(item.key) != 32
                or item.status not in {"active", "retired"}
                or item.fingerprint != hashlib.sha256(item.key).hexdigest()
            ):
                raise ValueError("archive keyring key material is inconsistent")
        by_id = {item.key_id: item for item in keys}
        if len(by_id) != len(keys) or active_key_id not in by_id:
            raise ValueError("archive keyring key IDs are invalid")
        if legacy_key_id is not None and legacy_key_id not in by_id:
            raise ValueError("archive keyring legacy key is unavailable")
        if sum(item.status == "active" for item in keys) != 1:
            raise ValueError("archive keyring requires exactly one active key")
        if by_id[active_key_id].status != "active":
            raise ValueError("archive keyring active key metadata is inconsistent")
        self.generation = generation
        self.active_key_id = active_key_id
        self.legacy_key_id = legacy_key_id
        self._keys = by_id

    @classmethod
    def configured(cls) -> bool:
        return bool(
            os.getenv("NOYRA_ARCHIVE_KEYRING_PATH", "").strip()
            or os.getenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", "").strip()
        )

    @classmethod
    def from_env(cls) -> ArchiveKeyring:
        keyring_path = os.getenv("NOYRA_ARCHIVE_KEYRING_PATH", "").strip()
        legacy_value = os.getenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", "").strip()
        if keyring_path and legacy_value:
            raise ValueError("configure either the archive keyring or the legacy archive key")
        if keyring_path:
            return cls.from_path(keyring_path)
        if not legacy_value:
            raise ArchiveKeyUnavailableError("archive encryption keyring is unavailable")
        key = cls._decode_key(legacy_value)
        fingerprint = hashlib.sha256(key).hexdigest()
        key_id = os.getenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "").strip() or fingerprint[:16]
        cls._validate_key_id(key_id)
        material = ArchiveKeyMaterial(key_id, key, fingerprint, "active")
        return cls(generation=1, active_key_id=key_id, legacy_key_id=key_id, keys=(material,))

    @classmethod
    def from_path(cls, path: Path | str) -> ArchiveKeyring:
        try:
            resolved = validate_keyring_path(path)
        except AtRestError as error:
            raise ArchiveKeyUnavailableError("archive keyring path or ACL is unsafe") from error
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_ARCHIVE_KEYRING_BYTES + 1)
        if not raw or len(raw) > MAX_ARCHIVE_KEYRING_BYTES:
            raise ValueError("archive keyring file size is invalid")
        try:
            payload = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError) as error:
            raise ValueError("archive keyring JSON is invalid") from error
        if not isinstance(payload, dict) or set(payload) != {
            "format",
            "generation",
            "active_key_id",
            "legacy_key_id",
            "keys",
        }:
            raise ValueError("archive keyring structure is invalid")
        if payload["format"] != ARCHIVE_KEYRING_FORMAT:
            raise ValueError("archive keyring format is unsupported")
        generation = payload["generation"]
        active_key_id = payload["active_key_id"]
        legacy_key_id = payload["legacy_key_id"]
        raw_keys = payload["keys"]
        if (
            type(generation) is not int
            or not isinstance(active_key_id, str)
            or (legacy_key_id is not None and not isinstance(legacy_key_id, str))
            or not isinstance(raw_keys, list)
        ):
            raise ValueError("archive keyring fields are invalid")
        cls._validate_key_id(active_key_id)
        if legacy_key_id is not None:
            cls._validate_key_id(legacy_key_id)
        materials: list[ArchiveKeyMaterial] = []
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict) or set(raw_key) != {"key_id", "status", "key_b64"}:
                raise ValueError("archive keyring key entry is invalid")
            key_id = raw_key["key_id"]
            status = raw_key["status"]
            encoded = raw_key["key_b64"]
            if not isinstance(key_id, str) or status not in {"active", "retired"}:
                raise ValueError("archive keyring key metadata is invalid")
            if not isinstance(encoded, str):
                raise ValueError("archive keyring key material is invalid")
            cls._validate_key_id(key_id)
            key = cls._decode_key(encoded)
            materials.append(
                ArchiveKeyMaterial(key_id, key, hashlib.sha256(key).hexdigest(), status)
            )
        return cls(
            generation=generation,
            active_key_id=active_key_id,
            legacy_key_id=legacy_key_id,
            keys=tuple(materials),
        )

    @property
    def active(self) -> ArchiveKeyMaterial:
        return self._keys[self.active_key_id]

    def material_for_key_id(self, key_id: object) -> ArchiveKeyMaterial:
        if not isinstance(key_id, str):
            raise ArchiveKeyUnavailableError("archive encryption key ID is unavailable")
        material = self._keys.get(key_id)
        if material is None:
            raise ArchiveKeyUnavailableError(f"archive encryption key is unavailable: {key_id}")
        return material

    def resolve(self, key_id: object, fingerprint: object) -> ArchiveKeyMaterial:
        if key_id is None and fingerprint is None:
            if self.legacy_key_id is None:
                raise ArchiveKeyUnavailableError("legacy archive encryption key is unavailable")
            return self._keys[self.legacy_key_id]
        if key_id is None or fingerprint is None:
            raise IntegrityError("archive encryption key metadata is incomplete")
        if not isinstance(key_id, str) or not isinstance(fingerprint, str):
            raise IntegrityError("archive encryption key metadata is invalid")
        material = self._keys.get(key_id)
        if material is None or material.fingerprint != fingerprint:
            raise ArchiveKeyUnavailableError(f"archive encryption key is unavailable: {key_id}")
        return material

    def provider(
        self,
        root: Path | str,
        *,
        key_id: object,
        fingerprint: object,
        create_root: bool,
    ) -> LocalArchiveProvider:
        material = self.resolve(key_id, fingerprint)
        return LocalArchiveProvider(
            root,
            encryption_key=material.key,
            key_id=material.key_id,
            create_root=create_root,
        )

    def metadata(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "key_id": item.key_id,
                "fingerprint": item.fingerprint,
                "status": item.status,
            }
            for item in sorted(self._keys.values(), key=lambda value: value.key_id)
        )

    def record_revision(self, database: Database, subject_id: str) -> bool:
        validate_subject_id(subject_id)
        metadata = self.metadata()
        metadata_json = canonical_json(list(metadata))
        metadata_hash = content_hash(list(metadata))
        with database.transaction() as connection:
            if self._validate_revision_connection(
                connection,
                subject_id,
                metadata=metadata,
                metadata_hash=metadata_hash,
            ):
                return False
            connection.execute(
                "INSERT INTO archive_keyring_revisions(revision_id, subject_id, generation, "
                "format, active_key_id, legacy_key_id, key_metadata_json, metadata_hash, "
                "state_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("archive-keyring-rev"),
                    subject_id,
                    self.generation,
                    ARCHIVE_KEYRING_FORMAT,
                    self.active_key_id,
                    self.legacy_key_id,
                    metadata_json,
                    metadata_hash,
                    content_hash(self._revision_state(metadata)),
                    utc_now(),
                ),
            )
        return True

    def validate_revision(
        self,
        database: Database,
        subject_id: str,
        *,
        connection: Any | None = None,
    ) -> None:
        """Reject rollback or key-ID reuse without mutating a read path."""
        validate_subject_id(subject_id)
        metadata = self.metadata()
        if connection is not None:
            self._validate_revision_connection(
                connection,
                subject_id,
                metadata=metadata,
                metadata_hash=content_hash(list(metadata)),
            )
            return
        with database.connection() as connection:
            self._validate_revision_connection(
                connection,
                subject_id,
                metadata=metadata,
                metadata_hash=content_hash(list(metadata)),
            )

    def _validate_revision_connection(
        self,
        connection: Any,
        subject_id: str,
        *,
        metadata: tuple[dict[str, str], ...],
        metadata_hash: str,
    ) -> bool:
        validate_subject_id(subject_id)
        revisions = connection.execute(
            "SELECT * FROM archive_keyring_revisions ORDER BY generation, subject_id"
        )
        historical: dict[str, str] = {}
        generation_states: dict[int, tuple[str, str, str | None]] = {}
        latest_global: tuple[int, Any] | None = None
        latest_subject: tuple[int, Any] | None = None
        for row in revisions:
            generation, entries = self._validated_revision(row)
            state = (
                str(row["metadata_hash"]),
                str(row["active_key_id"]),
                None if row["legacy_key_id"] is None else str(row["legacy_key_id"]),
            )
            previous_state = generation_states.setdefault(generation, state)
            if previous_state != state:
                raise IntegrityError("archive keyring generation has conflicting global metadata")
            latest_global = (generation, row)
            if row["subject_id"] == subject_id:
                latest_subject = (generation, row)
            for entry in entries:
                key_id = entry["key_id"]
                fingerprint = entry["fingerprint"]
                recorded_fingerprint = historical.setdefault(key_id, fingerprint)
                if recorded_fingerprint != fingerprint:
                    raise IntegrityError("archive key ID has conflicting historical fingerprints")

        generations = sorted(generation_states)
        if any(current != previous + 1 for previous, current in pairwise(generations)):
            raise IntegrityError("archive keyring global generation history has a gap")

        current_state = (metadata_hash, self.active_key_id, self.legacy_key_id)
        recorded_generation_state = generation_states.get(self.generation)
        if recorded_generation_state is not None and recorded_generation_state != current_state:
            raise ArchiveKeyUnavailableError(
                "archive keyring generation identifies different global key metadata"
            )
        if latest_global is not None and latest_global[0] > self.generation:
            raise ArchiveKeyUnavailableError("archive keyring generation rolled back")
        if (
            latest_global is not None
            and self.generation > latest_global[0]
            and self.generation != latest_global[0] + 1
        ):
            raise ArchiveKeyUnavailableError("archive keyring global generation is not sequential")

        identical_revision = False
        if latest_subject is not None:
            latest_generation, _latest = latest_subject
            if latest_generation == self.generation:
                identical_revision = True
            elif latest_generation > self.generation:
                raise ArchiveKeyUnavailableError("archive keyring generation rolled back")

        for entry in metadata:
            existing_fingerprint = historical.get(entry["key_id"])
            if existing_fingerprint is not None and existing_fingerprint != entry["fingerprint"]:
                raise ArchiveKeyUnavailableError("archive key ID cannot be reused")

        bindings = dict(historical)
        for entry in metadata:
            bindings.setdefault(entry["key_id"], entry["fingerprint"])

        referenced = connection.execute(
            "SELECT encryption_key_id, encryption_key_fingerprint FROM event_payload_segments "
            "UNION SELECT encryption_key_id, encryption_key_fingerprint "
            "FROM observation_content_segments"
        )
        for row in referenced:
            if row["encryption_key_id"] is None and row["encryption_key_fingerprint"] is None:
                if self.legacy_key_id is None:
                    raise ArchiveKeyUnavailableError("legacy archive segments require a legacy key")
                if (
                    latest_global is not None
                    and latest_global[1]["legacy_key_id"] is not None
                    and latest_global[1]["legacy_key_id"] != self.legacy_key_id
                ):
                    raise ArchiveKeyUnavailableError(
                        "legacy archive key cannot change while legacy segments remain"
                    )
                continue
            key_id = row["encryption_key_id"]
            fingerprint = row["encryption_key_fingerprint"]
            if key_id is None or fingerprint is None:
                raise IntegrityError("archive encryption key metadata is incomplete")
            if not isinstance(key_id, str) or not isinstance(fingerprint, str):
                raise IntegrityError("archive encryption key metadata is invalid")
            referenced_fingerprint = bindings.get(key_id)
            if referenced_fingerprint is None:
                raise IntegrityError("archive segment references an unrecorded key ID")
            if referenced_fingerprint != fingerprint:
                raise IntegrityError("archive segment key fingerprint conflicts with key history")
            self.resolve(key_id, fingerprint)
        return identical_revision

    @classmethod
    def _validated_revision(cls, row: Any) -> tuple[int, tuple[dict[str, str], ...]]:
        """Authenticate one append-only keyring metadata snapshot."""
        try:
            generation = strict_int(row["generation"])
            entries = strict_json_loads(row["key_metadata_json"])
        except (KeyError, OverflowError, TypeError, ValueError) as error:
            raise IntegrityError("archive keyring revision metadata is invalid") from error
        if generation < 1 or row["format"] != ARCHIVE_KEYRING_FORMAT:
            raise IntegrityError("archive keyring revision metadata is invalid")
        if not isinstance(entries, list) or not entries:
            raise IntegrityError("archive keyring revision metadata is invalid")
        metadata: list[dict[str, str]] = []
        key_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"key_id", "fingerprint", "status"}:
                raise IntegrityError("archive keyring revision metadata is invalid")
            key_id = entry["key_id"]
            fingerprint = entry["fingerprint"]
            status = entry["status"]
            if (
                not isinstance(key_id, str)
                or not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                or status not in {"active", "retired"}
                or key_id in key_ids
            ):
                raise IntegrityError("archive keyring revision metadata is invalid")
            try:
                cls._validate_key_id(key_id)
            except ValueError as error:
                raise IntegrityError("archive keyring revision metadata is invalid") from error
            key_ids.add(key_id)
            metadata.append({"key_id": key_id, "fingerprint": fingerprint, "status": status})
        if metadata != sorted(metadata, key=lambda entry: entry["key_id"]):
            raise IntegrityError("archive keyring revision metadata is invalid")
        active_key_id = row["active_key_id"]
        legacy_key_id = row["legacy_key_id"]
        active = [entry["key_id"] for entry in metadata if entry["status"] == "active"]
        if (
            not isinstance(active_key_id, str)
            or active != [active_key_id]
            or (
                legacy_key_id is not None
                and (not isinstance(legacy_key_id, str) or legacy_key_id not in key_ids)
            )
        ):
            raise IntegrityError("archive keyring revision metadata is invalid")
        metadata_tuple = tuple(metadata)
        expected_metadata_hash = content_hash(list(metadata_tuple))
        expected_state_hash = content_hash(
            {
                "generation": generation,
                "format": ARCHIVE_KEYRING_FORMAT,
                "active_key_id": active_key_id,
                "legacy_key_id": legacy_key_id,
                "key_metadata": list(metadata_tuple),
            }
        )
        if (
            row["metadata_hash"] != expected_metadata_hash
            or row["state_hash"] != expected_state_hash
        ):
            raise IntegrityError("archive keyring revision hash is invalid")
        return generation, metadata_tuple

    def _revision_state(self, metadata: tuple[dict[str, str], ...]) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "format": ARCHIVE_KEYRING_FORMAT,
            "active_key_id": self.active_key_id,
            "legacy_key_id": self.legacy_key_id,
            "key_metadata": list(metadata),
        }

    @staticmethod
    def _validate_key_id(key_id: str) -> None:
        if _ARCHIVE_KEY_ID.fullmatch(key_id) is None:
            raise ValueError("archive key ID is invalid")

    @staticmethod
    def _decode_key(value: str) -> bytes:
        try:
            key = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeError, ValueError) as error:
            raise ValueError("archive encryption key is invalid") from error
        if len(key) != 32 or base64.urlsafe_b64encode(key).decode("ascii") != value:
            raise ValueError("archive encryption key must be canonical base64url AES-256 material")
        return key


class ArchiveProvider(Protocol):
    name: str

    def put(self, object_key: str, payload: bytes) -> str: ...

    def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes: ...

    def exists(self, object_key: str) -> bool: ...


def resolve_subject_archive_root(
    database: Database,
    subject_id: str,
    root: Path | str,
    *,
    create: bool = False,
) -> Path:
    """Resolve the keyed archive directory while retaining legacy fallback.

    Production callers pass ``<data-root>/subject/cold``.  The physical
    archive is stored below the immutable subject storage key.  Direct callers
    that already pass a concrete archive directory (including older fixtures)
    keep that directory when it contains legacy data, so migration is gradual
    and never silently moves an existing object tree during a read.
    """
    # Validate the un-resolved path first.  Resolving a symlink/junction before
    # checking it would turn an archive root that escapes the data directory
    # into an apparently trusted absolute path and bypass the provider's
    # parent-link checks.
    configured_input = Path(root).expanduser()
    _assert_archive_root_has_no_reparse_points(configured_input)
    configured = configured_input.resolve()
    # When callers pass an already-keyed ``subject/<storage_key>/cold`` path,
    # bind that directory to the requested subject as well.  Without this
    # check a caller could accidentally read/write another subject's archive
    # while all SQL predicates still use the current subject ID.
    if configured.name == "cold" and configured.parent.parent.name == "subject":
        expected_storage_key = IdentityStore(database).storage_key(subject_id)
        if configured.parent.name != expected_storage_key:
            raise IntegrityError("archive root subject binding mismatch")
    subject_base = configured
    if configured.name == "cold" and configured.parent.name == "subject":
        subject_base = configured.parent
    elif configured.name == "subject":
        subject_base = configured
    else:
        with database.connection() as connection:
            subject_count = int(
                connection.execute("SELECT COUNT(*) FROM subject_identity").fetchone()[0]
            )
        if subject_count > 1:
            raise IntegrityError(
                "arbitrary archive root is not subject-scoped in a multi-subject database"
            )
        return configured
    if configured.name == "cold" and configured.is_dir():
        try:
            has_legacy_content = next(configured.iterdir(), None) is not None
        except OSError as error:
            raise IntegrityError("legacy archive root cannot be inspected") from error
        if has_legacy_content:
            with database.connection() as connection:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM subject_identity").fetchone()[0]
                )
            if count != 1:
                raise IntegrityError("shared legacy archive root is ambiguous")
            return configured
    identities = IdentityStore(database)
    storage_key = identities.storage_key(subject_id)
    legacy_allowed = identities.legacy_storage_path_is_unambiguous(subject_id)
    keyed = SubjectStorageDirectory.locate(
        subject_base,
        storage_key,
        legacy_subject_id=subject_id,
        legacy_migration_allowed=legacy_allowed,
        create=create,
    )
    if keyed is not None:
        keyed_archive = keyed / "cold"
        if keyed_archive.exists() or create:
            if create:
                keyed_archive.mkdir(parents=True, exist_ok=True)
            return keyed_archive
    # Legacy direct roots remain readable until a successful write migrates
    # them.  This branch is deliberately after keyed lookup.
    if configured.exists() or not create:
        return configured
    configured.mkdir(parents=True, exist_ok=True)
    return configured


def _assert_archive_root_has_no_reparse_points(path: Path) -> None:
    """Reject symlink/junction components before canonicalizing an archive root."""
    try:
        absolute = path.absolute()
    except OSError as error:
        raise IntegrityError("archive root cannot be resolved") from error
    current = Path(absolute.anchor) if absolute.anchor else Path()
    for part in absolute.parts[1:] if absolute.anchor else absolute.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # Missing leaf/ancestors are safe to create below the validated
            # existing boundary; later provider writes re-check each parent.
            break
        except OSError as error:
            raise IntegrityError("archive root cannot be inspected") from error
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise IntegrityError("archive root cannot contain a symbolic link or reparse point")


class ArchiveStagingLedger:
    """Durable two-phase ledger for local archive object publication.

    Archive providers are outside SQLite's transaction boundary.  A manifest
    therefore records the immutable source selection before the provider write,
    then records the stored object's size/hash, and is marked ``committed`` in
    the same transaction that publishes source-row pointers.  Startup and
    maintenance can safely replay ``prepared``/``stored`` manifests; an object
    is deleted only after the database proves that no committed pointer or live
    staging manifest references it.
    """

    def __init__(self, database: Database):
        self.database = database

    def prepare(
        self,
        subject_id: str,
        *,
        archive_kind: str,
        segment_id: str,
        object_key: str,
        source_state: Any,
        item_count: int,
        first_item_at: str,
        last_item_at: str,
        plaintext_hash: str,
        archive_format: str,
        encryption_key_id: str,
        encryption_key_fingerprint: str,
    ) -> str:
        if item_count < 1:
            raise ValueError("archive staging item count must be positive")
        source_json = canonical_json(source_state)
        source_hash = content_hash(source_state)
        now = utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM archive_staging_manifests WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["subject_id"]) != subject_id
                    or str(existing["archive_kind"]) != archive_kind
                    or str(existing["object_key"]) != object_key
                    or str(existing["source_state_json"]) != source_json
                    or str(existing["source_state_hash"]) != source_hash
                    or int(existing["item_count"]) != item_count
                    or str(existing["first_item_at"]) != first_item_at
                    or str(existing["last_item_at"]) != last_item_at
                    or str(existing["plaintext_hash"]) != plaintext_hash
                    or str(existing["archive_format"]) != archive_format
                    or str(existing["encryption_key_id"]) != encryption_key_id
                    or str(existing["encryption_key_fingerprint"]) != encryption_key_fingerprint
                ):
                    raise IntegrityError("archive staging segment identity changed")
                return str(existing["manifest_id"])
            committed_collision = connection.execute(
                "SELECT 1 FROM event_payload_segments WHERE segment_id = ? "
                "UNION ALL SELECT 1 FROM observation_content_segments WHERE segment_id = ? "
                "LIMIT 1",
                (segment_id, segment_id),
            ).fetchone()
            if committed_collision is not None:
                raise IntegrityError("archive staging segment is already committed")
            collision = connection.execute(
                "SELECT manifest_id FROM archive_staging_manifests WHERE object_key = ?",
                (object_key,),
            ).fetchone()
            if collision is not None:
                raise IntegrityError("archive staging object key is already bound")
            manifest_id = new_id("archive-manifest")
            connection.execute(
                """INSERT INTO archive_staging_manifests(
                    manifest_id, subject_id, archive_kind, segment_id, object_key,
                    source_state_json, source_state_hash, item_count, first_item_at,
                    last_item_at, plaintext_hash, archive_format, encryption_key_id,
                    encryption_key_fingerprint, stored_byte_size, stored_hash, status,
                    last_error, created_at, updated_at, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                          'prepared', NULL, ?, ?, NULL)""",
                (
                    manifest_id,
                    subject_id,
                    archive_kind,
                    segment_id,
                    object_key,
                    source_json,
                    source_hash,
                    item_count,
                    first_item_at,
                    last_item_at,
                    plaintext_hash,
                    archive_format,
                    encryption_key_id,
                    encryption_key_fingerprint,
                    now,
                    now,
                ),
            )
            return manifest_id

    def mark_stored(self, manifest_id: str, *, byte_size: int, stored_hash: str) -> None:
        if byte_size < 0 or not stored_hash.strip():
            raise ValueError("archive staging stored metadata is invalid")
        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE archive_staging_manifests SET status = 'stored', "
                "stored_byte_size = ?, stored_hash = ?, last_error = NULL, updated_at = ? "
                "WHERE manifest_id = ? AND status = 'prepared'",
                (byte_size, stored_hash, now, manifest_id),
            )
            if changed.rowcount == 0:
                row = connection.execute(
                    "SELECT status, stored_byte_size, stored_hash FROM archive_staging_manifests "
                    "WHERE manifest_id = ?",
                    (manifest_id,),
                ).fetchone()
                if row is None:
                    raise IntegrityError("archive staging manifest is missing")
                if row["status"] == "stored" and (
                    int(row["stored_byte_size"]) != byte_size
                    or str(row["stored_hash"]) != stored_hash
                ):
                    raise IntegrityError("archive staging stored hash changed")
                if row["status"] not in {"stored", "committed"}:
                    raise IntegrityError("archive staging manifest is not writable")

    def get(self, manifest_id: str) -> Any:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM archive_staging_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()

    def mark_abandoned(self, manifest_id: str, error_code: str) -> bool:
        """CAS a live manifest to abandoned.

        Callers may delete the external object only when this returns true. A
        concurrent finalizer can move the manifest to ``committed`` after a
        replay worker read its stale row; deleting after a failed CAS would
        destroy a now-authoritative archive object.
        """
        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE archive_staging_manifests SET status = 'abandoned', "
                "last_error = ?, updated_at = ? WHERE manifest_id = ? "
                "AND status IN ('prepared', 'stored')",
                (error_code[:256], now, manifest_id),
            )
        return changed.rowcount == 1

    def mark_removed(self, manifest_id: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE archive_staging_manifests SET status = 'removed', "
                "updated_at = ?, finalized_at = COALESCE(finalized_at, ?) "
                "WHERE manifest_id = ? AND status = 'abandoned'",
                (now, now, manifest_id),
            )

    def referenced(self, object_key: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM archive_staging_manifests WHERE object_key = ? "
                "AND status IN ('prepared', 'stored', 'committed') LIMIT 1",
                (object_key,),
            ).fetchone()
            if row is not None:
                return True
            row = connection.execute(
                "SELECT 1 FROM event_payload_segments WHERE object_key = ? LIMIT 1",
                (object_key,),
            ).fetchone()
            if row is not None:
                return True
            return (
                connection.execute(
                    "SELECT 1 FROM observation_content_segments WHERE object_key = ? LIMIT 1",
                    (object_key,),
                ).fetchone()
                is not None
            )


class LocalArchiveProvider:
    name = "local"

    def __init__(
        self,
        root: Path | str,
        *,
        encryption_key: bytes | None = None,
        key_id: str | None = None,
        allow_unencrypted: bool = False,
        create_root: bool = True,
    ):
        self.root = Path(root).expanduser().resolve()
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True)
        configured = os.getenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", "").strip()
        if encryption_key is None and configured:
            try:
                encryption_key = base64.urlsafe_b64decode(configured)
            except ValueError as error:
                raise ValueError("archive encryption key is invalid") from error
        if encryption_key is None and not allow_unencrypted:
            raise ValueError("local archives require an external AES-256 encryption key")
        if encryption_key is not None and len(encryption_key) != 32:
            raise ValueError("archive encryption key must contain 32 bytes")
        self._encryption_key = encryption_key
        self.key_fingerprint = (
            hashlib.sha256(encryption_key).hexdigest() if encryption_key is not None else None
        )
        configured_key_id = os.getenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "").strip()
        self.key_id = (
            key_id
            or configured_key_id
            or (self.key_fingerprint[:16] if self.key_fingerprint is not None else None)
        )
        if self.key_id is not None:
            ArchiveKeyring._validate_key_id(self.key_id)

    def _path(self, object_key: str) -> Path:
        if not object_key.strip() or "\\" in object_key:
            raise ValueError("archive object key must be a nonblank POSIX path")
        relative = PurePosixPath(object_key)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or any(":" in part for part in relative.parts)
        ):
            raise ValueError("archive object key escapes provider root")
        target = self.root.joinpath(*relative.parts)
        if target == self.root:
            raise ValueError("archive object key must identify a file")
        return target

    def put(self, object_key: str, payload: bytes) -> str:
        stored = self._encrypt(object_key, payload)
        self.put_stored(object_key, stored)
        return hashlib.sha256(payload).hexdigest()

    def put_stored(self, object_key: str, payload: bytes) -> str:
        target = self._path(object_key)
        self._assert_no_symlink_parents(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink_parents(target)
        temporary = target.with_name(f".{new_id('awrite')}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("archive write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            self._fsync_parent_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _fsync_parent_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        payload = self.get_stored(object_key, max_bytes=self._stored_limit(max_bytes))
        return self._decrypt(object_key, payload)

    def get_stored(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        path = self._path(object_key)
        self._assert_no_symlink_parents(path)
        if max_bytes is not None:
            if max_bytes < 1:
                raise ValueError("archive read limit must be positive")
            with path.open("rb") as stream:
                payload = stream.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise PayloadLimitError("archive object exceeds the configured read limit")
        else:
            payload = path.read_bytes()
        return payload

    def decrypt_stored(self, object_key: str, payload: bytes) -> bytes:
        return self._decrypt(object_key, payload)

    def stored_metadata(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
    ) -> tuple[int, str]:
        """Hash stored bytes without materializing the entire archive object."""
        path = self._path(object_key)
        self._assert_no_symlink_parents(path)
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("archive metadata read limit must be positive")
        expected_size = path.stat().st_size
        if max_bytes is not None and expected_size > max_bytes:
            raise PayloadLimitError("archive object exceeds the configured read limit")
        digest = hashlib.sha256()
        byte_size = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                byte_size += len(chunk)
                if max_bytes is not None and byte_size > max_bytes:
                    raise PayloadLimitError("archive object exceeds the configured read limit")
                digest.update(chunk)
        if byte_size != expected_size:
            raise IntegrityError("archive object size changed during metadata read")
        return byte_size, digest.hexdigest()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        self._assert_no_symlink_parents(path)
        path.unlink(missing_ok=True)

    def exists(self, object_key: str) -> bool:
        path = self._path(object_key)
        self._assert_no_symlink_parents(path)
        return path.is_file()

    def _stored_limit(self, plaintext_limit: int | None) -> int | None:
        if plaintext_limit is None:
            return None
        return plaintext_limit + (37 if self._encryption_key is not None else 0)

    def _encrypt(self, object_key: str, payload: bytes) -> bytes:
        if self._encryption_key is None:
            return payload
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce, payload, object_key.encode("utf-8")
        )
        return b"NOYRAENC1" + nonce + ciphertext

    def _decrypt(self, object_key: str, payload: bytes) -> bytes:
        if self._encryption_key is None:
            return payload
        if not payload.startswith(b"NOYRAENC1") or len(payload) < 22:
            raise ArchiveAuthenticationError("local archive is not encrypted")
        nonce = payload[9:21]
        try:
            return AESGCM(self._encryption_key).decrypt(
                nonce, payload[21:], object_key.encode("utf-8")
            )
        except Exception as error:
            raise ArchiveAuthenticationError("local archive authentication failed") from error

    def _assert_no_symlink_parents(self, target: Path) -> None:
        current = target.parent
        while current != self.root:
            if current.is_symlink():
                raise ValueError("archive path contains a symbolic link")
            current = current.parent
        if target.is_symlink():
            raise ValueError("archive target cannot be a symbolic link")


class S3ArchiveProvider:
    """S3-compatible archive provider using an injected client.

    The client is intentionally injected so credentials remain outside Noyra's
    subject database and the optional cloud dependency is not needed by the
    desktop build.
    """

    name = "s3"
    _global_sdk_call_gate: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(4)
    _global_body_close_gate: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(4)

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        prefix: str = "",
        attempts: int = 3,
        backoff_seconds: float = 0.2,
        server_side_encryption: str = "AES256",
        kms_key_id: str | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        account_id: str | None = None,
        operation_timeout_seconds: float = 30.0,
        readiness_ttl_seconds: float = 60.0,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30.0,
    ):
        if not bucket.strip() or attempts < 1 or backoff_seconds < 0:
            raise ValueError("invalid S3 archive configuration")
        if not 0 < operation_timeout_seconds <= 600:
            raise ValueError("S3 archive operation timeout is invalid")
        if not 1 <= readiness_ttl_seconds <= 3_600:
            raise ValueError("S3 archive readiness TTL is invalid")
        if not 1 <= circuit_failure_threshold <= 20:
            raise ValueError("S3 archive circuit threshold is invalid")
        if not 1 <= circuit_cooldown_seconds <= 3_600:
            raise ValueError("S3 archive circuit cooldown is invalid")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        if server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("S3 archive encryption must be AES256 or aws:kms")
        if server_side_encryption == "aws:kms" and not (kms_key_id and kms_key_id.strip()):
            raise ValueError("S3 KMS encryption requires a key ID")
        self.server_side_encryption = server_side_encryption
        self.kms_key_id = kms_key_id
        metadata = getattr(client, "meta", None)
        discovered_endpoint = endpoint_url or getattr(metadata, "endpoint_url", None)
        discovered_region = region_name or getattr(metadata, "region_name", None)
        discovered_account = (
            account_id
            or getattr(client, "account_id", None)
            or getattr(metadata, "account_id", None)
        )
        self.endpoint_url = self._canonical_endpoint(discovered_endpoint)
        self.region_name = str(discovered_region or "unknown").strip().lower()
        self.account_id = str(discovered_account or "unknown").strip()
        self.operation_timeout_seconds = operation_timeout_seconds
        self.readiness_ttl_seconds = readiness_ttl_seconds
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self._state_lock = threading.Lock()
        # A boto call that ignores its socket timeout is quarantined in this
        # single slot.  This prevents a slow/hung SDK from creating an
        # unbounded daemon-thread herd or allowing overlapping late writes.
        self._sdk_call_gate = threading.BoundedSemaphore(1)
        self._body_close_gate = threading.BoundedSemaphore(1)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_probe_required = False
        self._readiness_checked_at = 0.0
        self._readiness_status: dict[str, Any] | None = None
        self.provider_id = content_hash(
            {
                "provider": self.name,
                "endpoint_url": self.endpoint_url,
                "region_name": self.region_name,
                "account_id": self.account_id,
                "bucket": bucket,
                "prefix": self.prefix,
                "server_side_encryption": server_side_encryption,
                "kms_key_id": kms_key_id,
            }
        )

    def _owner_parameters(self) -> dict[str, Any]:
        """Bind S3 requests to the configured bucket owner when available.

        ``account_id`` is not merely part of the local provider hash: AWS S3
        validates ``ExpectedBucketOwner`` server-side and rejects credentials
        that resolve the same bucket name in a different account.  Compatible
        endpoints that do not implement this request contract fail readiness
        closed instead of silently weakening the identity binding.
        """
        if self.account_id == "unknown":
            return {}
        return {"ExpectedBucketOwner": self.account_id}

    def put(self, object_key: str, payload: bytes) -> str:
        key = self._key(object_key)
        digest = hashlib.sha256(payload).hexdigest()
        deadline = time.monotonic() + self.operation_timeout_seconds
        encryption_args: dict[str, Any] = {"ServerSideEncryption": self.server_side_encryption}
        if self.kms_key_id is not None:
            encryption_args["SSEKMSKeyId"] = self.kms_key_id
        # Archive payloads are already bounded in memory.  Using boto3's
        # upload_fileobj would create an internal s3transfer thread pool outside
        # Noyra's provider/global worker limits, so publish through one SDK call.
        self._call(
            self.client.put_object,
            deadline=deadline,
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            Metadata={"sha256": digest},
            **self._owner_parameters(),
            **encryption_args,
        )
        return digest

    def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("archive read limit must be positive")
        deadline = time.monotonic() + self.operation_timeout_seconds
        try:
            response = self._call(
                self.client.get_object,
                deadline=deadline,
                Bucket=self.bucket,
                Key=self._key(object_key),
                **self._owner_parameters(),
            )
        except Exception as error:
            if self._is_missing_error(error):
                raise FileNotFoundError(f"archive object is missing: {object_key}") from error
            raise
        body = response["Body"]
        try:
            if hasattr(body, "read"):
                if max_bytes is None:
                    payload = bytes(self._invoke_until_deadline(body.read, deadline))
                else:
                    chunks = bytearray()
                    while len(chunks) <= max_bytes:
                        chunk = self._invoke_until_deadline(
                            body.read,
                            deadline,
                            min(1024 * 1024, max_bytes + 1 - len(chunks)),
                        )
                        if not chunk:
                            break
                        if len(chunk) > max_bytes + 1 - len(chunks):
                            raise PayloadLimitError(
                                "archive object exceeds the configured read limit"
                            )
                        chunks.extend(chunk)
                    payload = bytes(chunks)
            else:
                payload = bytes(body)
        finally:
            self._close_response_body(body, deadline)
        if max_bytes is not None and len(payload) > max_bytes:
            raise PayloadLimitError("archive object exceeds the configured read limit")
        expected = response.get("Metadata", {}).get("sha256")
        if expected is not None and expected != hashlib.sha256(payload).hexdigest():
            raise IntegrityError("S3 archive checksum mismatch")
        return payload

    def exists(self, object_key: str) -> bool:
        try:
            self._call(
                self.client.head_object,
                deadline=time.monotonic() + self.operation_timeout_seconds,
                Bucket=self.bucket,
                Key=self._key(object_key),
                **self._owner_parameters(),
            )
        except Exception:
            return False
        return True

    def readiness(self, *, force: bool = False) -> dict[str, Any]:
        """Return cached, verified provider readiness without treating construction as proof."""
        now = time.monotonic()
        with self._state_lock:
            if not force and self._circuit_probe_required and self._circuit_open_until > now:
                if self._readiness_status is not None:
                    return dict(self._readiness_status)
                return {
                    **self.identity(),
                    "ready": False,
                    "last_checked_at": utc_now(),
                    "error": "circuit_open",
                }
            probe_due = self._circuit_probe_required and self._circuit_open_until <= now
            if (
                not force
                and not probe_due
                and self._readiness_status is not None
                and now - self._readiness_checked_at < self.readiness_ttl_seconds
            ):
                return dict(self._readiness_status)
        try:
            self._call(
                self.client.head_bucket,
                deadline=now + self.operation_timeout_seconds,
                bypass_circuit=True,
                Bucket=self.bucket,
                **self._owner_parameters(),
            )
        except Exception as error:
            status: dict[str, Any] = {
                **self.identity(),
                "ready": False,
                "last_checked_at": utc_now(),
                "error": type(error).__name__,
            }
        else:
            status = {
                **self.identity(),
                "ready": True,
                "last_checked_at": utc_now(),
                "error": None,
            }
        self._set_readiness_status(status)
        return dict(status)

    def _set_readiness_status(self, status: dict[str, Any]) -> None:
        with self._state_lock:
            self._readiness_checked_at = time.monotonic()
            self._readiness_status = status

    def identity(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "endpoint_url": self.endpoint_url,
            "region_name": self.region_name,
            "account_id": self.account_id,
            "bucket": self.bucket,
            "prefix": self.prefix,
        }

    def _key(self, object_key: str) -> str:
        if not object_key.strip() or "\\" in object_key or object_key.startswith("/"):
            raise ValueError("archive object key must be a relative POSIX path")
        key = f"{self.prefix}/{object_key}" if self.prefix else object_key
        if ".." in Path(key).parts:
            raise ValueError("archive object key escapes provider prefix")
        return key

    def _call(
        self,
        function: Any,
        *,
        deadline: float | None = None,
        bypass_circuit: bool = False,
        **kwargs: Any,
    ) -> Any:
        end = deadline or (time.monotonic() + self.operation_timeout_seconds)
        if not bypass_circuit:
            self._require_closed_circuit()
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                result = self._invoke_until_deadline(function, end, **kwargs)
            except Exception as error:
                last = error
                if isinstance(error, (TimeoutError, ArchiveUnavailableError)):
                    break
                if attempt + 1 < self.attempts:
                    delay = self.backoff_seconds * (2**attempt)
                    remaining = end - time.monotonic()
                    if remaining <= delay:
                        last = TimeoutError("S3 archive operation deadline exceeded")
                        break
                    time.sleep(delay)
            else:
                if bypass_circuit:
                    self._record_readiness_success()
                else:
                    self._record_provider_success()
                return result
        assert last is not None
        if not self._is_missing_error(last):
            self._record_provider_failure()
        raise last

    def _invoke_until_deadline(
        self,
        function: Any,
        deadline: float,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("S3 archive operation deadline exceeded")
        if not self._global_sdk_call_gate.acquire(timeout=remaining):
            raise ArchiveUnavailableError("S3 archive process capacity is exhausted")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._sdk_call_gate.acquire(timeout=max(0.0, remaining)):
            self._global_sdk_call_gate.release()
            raise ArchiveUnavailableError("S3 archive provider call is still in progress")
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, function(*args, **kwargs)))
            except Exception as error:  # provider exceptions are returned to the caller
                outcome.put((False, error))
            finally:
                self._sdk_call_gate.release()
                self._global_sdk_call_gate.release()

        worker = threading.Thread(target=invoke, name="noyra-s3-call", daemon=True)
        try:
            worker.start()
        except BaseException:
            self._sdk_call_gate.release()
            self._global_sdk_call_gate.release()
            raise
        try:
            succeeded, value = outcome.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError("S3 archive operation deadline exceeded") from error
        if succeeded:
            return value
        raise value

    def _close_response_body(self, body: Any, deadline: float) -> None:
        """Close a GET body without allowing cleanup to violate the operation deadline.

        A timed-out ``read`` still owns the provider SDK slot.  Closing through
        that same slot can therefore never run, leaking the response and
        preventing the close from interrupting the blocked read.  Cleanup uses
        a separate, globally and per-provider bounded daemon slot.  The caller
        waits only for deadline headroom; after timeout, close continues in the
        bounded cleanup worker.
        """
        close = getattr(body, "close", None)
        if not callable(close):
            return
        if not self._global_body_close_gate.acquire(blocking=False):
            return
        if not self._body_close_gate.acquire(blocking=False):
            self._global_body_close_gate.release()
            return
        completed = threading.Event()

        def invoke_close() -> None:
            try:
                with suppress(Exception):
                    close()
            finally:
                completed.set()
                self._body_close_gate.release()
                self._global_body_close_gate.release()

        worker = threading.Thread(
            target=invoke_close,
            name="noyra-s3-body-close",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            self._body_close_gate.release()
            self._global_body_close_gate.release()
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            completed.wait(timeout=remaining)

    def _require_closed_circuit(self) -> None:
        now = time.monotonic()
        with self._state_lock:
            if self._circuit_open_until > now:
                raise ArchiveUnavailableError("S3 archive provider circuit is open")
            if self._circuit_probe_required:
                raise ArchiveUnavailableError("S3 archive provider requires a readiness probe")

    def _record_provider_success(self) -> None:
        with self._state_lock:
            self._consecutive_failures = 0
            self._readiness_checked_at = time.monotonic()
            self._readiness_status = {
                **self.identity(),
                "ready": True,
                "last_checked_at": utc_now(),
                "error": None,
            }

    def _record_readiness_success(self) -> None:
        with self._state_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._circuit_probe_required = False
            self._readiness_checked_at = time.monotonic()
            self._readiness_status = {
                **self.identity(),
                "ready": True,
                "last_checked_at": utc_now(),
                "error": None,
            }

    def _record_provider_failure(self) -> None:
        with self._state_lock:
            self._consecutive_failures += 1
            self._readiness_checked_at = time.monotonic()
            self._readiness_status = {
                **self.identity(),
                "ready": False,
                "last_checked_at": utc_now(),
                "error": "provider_failure",
            }
            if self._consecutive_failures >= self.circuit_failure_threshold:
                self._circuit_open_until = time.monotonic() + self.circuit_cooldown_seconds
                self._circuit_probe_required = True

    @staticmethod
    def _canonical_endpoint(endpoint: Any) -> str:
        if endpoint is None or not str(endpoint).strip():
            return "aws-default"
        parsed = urlsplit(str(endpoint).strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("S3 endpoint URL is invalid")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("S3 endpoint URL cannot contain credentials, query, or fragment")
        raw_host = parsed.hostname
        try:
            host = str(ipaddress.ip_address(raw_host)).lower()
        except ValueError:
            try:
                host = raw_host.encode("idna").decode("ascii").lower()
            except UnicodeError as error:
                raise ValueError("S3 endpoint hostname is invalid") from error
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("S3 endpoint URL port is invalid") from error
        default_port = 443 if parsed.scheme == "https" else 80
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != default_port:
            netloc = f"{netloc}:{port}"
        return urlunsplit(
            SplitResult(
                parsed.scheme.lower(),
                netloc,
                parsed.path.rstrip("/"),
                "",
                "",
            )
        )

    @staticmethod
    def _is_missing_error(error: Exception) -> bool:
        if isinstance(error, KeyError):
            return True
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return False
        error_detail = response.get("Error")
        if not isinstance(error_detail, dict):
            return False
        return str(error_detail.get("Code", "")) in {"404", "NoSuchKey", "NotFound"}

    @classmethod
    def from_env(cls) -> S3ArchiveProvider:
        bucket = os.getenv("NOYRA_ARCHIVE_S3_BUCKET", "").strip()
        if not bucket:
            raise ValueError("NOYRA_ARCHIVE_S3_BUCKET is required")
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("install Noyra's optional cloud dependency") from error
        endpoint = os.getenv("NOYRA_ARCHIVE_S3_ENDPOINT") or None
        region = os.getenv("NOYRA_ARCHIVE_S3_REGION") or None
        account_id = os.getenv("NOYRA_ARCHIVE_S3_ACCOUNT_ID", "").strip()
        if not account_id:
            raise ValueError("NOYRA_ARCHIVE_S3_ACCOUNT_ID is required for stable provider identity")
        if endpoint is not None and urlsplit(endpoint).scheme.lower() != "https":
            raise ValueError("NOYRA_ARCHIVE_S3_ENDPOINT must use HTTPS")
        try:
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("install Noyra's optional cloud dependency") from error
        connect_timeout = float(os.getenv("NOYRA_ARCHIVE_S3_CONNECT_TIMEOUT_SECONDS", "5"))
        read_timeout = float(os.getenv("NOYRA_ARCHIVE_S3_READ_TIMEOUT_SECONDS", "20"))
        operation_timeout = float(os.getenv("NOYRA_ARCHIVE_S3_OPERATION_TIMEOUT_SECONDS", "30"))
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries={"max_attempts": 0, "mode": "standard"},
                max_pool_connections=4,
            ),
        )
        return cls(
            client,
            bucket=bucket,
            prefix=os.getenv("NOYRA_ARCHIVE_S3_PREFIX", ""),
            attempts=int(os.getenv("NOYRA_ARCHIVE_S3_ATTEMPTS", "3")),
            server_side_encryption=os.getenv("NOYRA_ARCHIVE_S3_SSE", "AES256"),
            kms_key_id=os.getenv("NOYRA_ARCHIVE_S3_KMS_KEY_ID") or None,
            endpoint_url=endpoint,
            region_name=region,
            account_id=account_id,
            operation_timeout_seconds=operation_timeout,
            readiness_ttl_seconds=float(os.getenv("NOYRA_ARCHIVE_S3_READINESS_TTL_SECONDS", "60")),
            circuit_failure_threshold=int(os.getenv("NOYRA_ARCHIVE_S3_CIRCUIT_FAILURES", "3")),
            circuit_cooldown_seconds=float(
                os.getenv("NOYRA_ARCHIVE_S3_CIRCUIT_COOLDOWN_SECONDS", "30")
            ),
        )


def archive_provider_id(provider: ArchiveProvider) -> str:
    configured = getattr(provider, "provider_id", None)
    if isinstance(configured, str) and configured.strip():
        return configured
    name = getattr(provider, "name", type(provider).__name__)
    return content_hash({"provider": str(name)})


def archive_object_kind(object_key: str) -> str:
    if object_key.startswith("events/"):
        return "event_segment"
    if object_key.startswith("observations/"):
        return "observation_segment"
    raise ValueError("archive object kind is unsupported")


def get_archive_payload(provider: ArchiveProvider, object_key: str, *, max_bytes: int) -> bytes:
    if max_bytes < 1:
        raise ValueError("archive read limit must be positive")
    try:
        supports_limit = "max_bytes" in inspect.signature(provider.get).parameters
    except (TypeError, ValueError):
        supports_limit = False
    if supports_limit:
        payload = provider.get(object_key, max_bytes=max_bytes)
    else:
        payload = provider.get(object_key)
    if len(payload) > max_bytes:
        raise PayloadLimitError("archive object exceeds the configured read limit")
    return payload


class ArchiveReplicaLedger:
    """Durable current replica state backed by append-only full-state revisions."""

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "pending": {"pending", "uploading", "unavailable", "missing", "corrupt"},
        "uploading": {"pending", "verified", "unavailable", "missing", "corrupt"},
        "present": {"present", "gc_pending", "absent", "restoring", "corrupt"},
        "verified": {"verified", "unavailable", "missing", "corrupt"},
        "gc_pending": {"present", "absent", "unavailable", "corrupt"},
        "absent": {"absent", "restoring", "unavailable", "missing", "corrupt"},
        "restoring": {"present", "absent", "unavailable", "missing", "corrupt"},
        "unavailable": {"pending", "uploading", "present", "verified", "absent", "restoring"},
        "missing": {"pending", "absent", "restoring", "verified", "corrupt"},
        "corrupt": {"pending", "absent", "restoring", "verified"},
    }

    def __init__(self, database: Database):
        self.database = database

    def ensure(
        self,
        subject_id: str,
        object_key: str,
        *,
        replica_type: str,
        provider_id: str,
        ciphertext_hash: str,
        byte_size: int,
        state: str,
        reason: str,
    ) -> Any:
        with self.database.transaction() as connection:
            return self._ensure_connection(
                connection,
                subject_id,
                object_key,
                replica_type=replica_type,
                provider_id=provider_id,
                ciphertext_hash=ciphertext_hash,
                byte_size=byte_size,
                state=state,
                reason=reason,
            )

    def _ensure_connection(
        self,
        connection: Any,
        subject_id: str,
        object_key: str,
        *,
        replica_type: str,
        provider_id: str,
        ciphertext_hash: str,
        byte_size: int,
        state: str,
        reason: str,
    ) -> Any:
        row = connection.execute(
            "SELECT * FROM archive_object_replicas WHERE subject_id = ? AND object_key = ? "
            "AND replica_type = ? AND provider_id = ?",
            (subject_id, object_key, replica_type, provider_id),
        ).fetchone()
        if row is not None:
            if (
                row["object_kind"] != archive_object_kind(object_key)
                or row["ciphertext_hash"] != ciphertext_hash
                or int(row["byte_size"]) != byte_size
            ):
                raise IntegrityError(f"archive replica immutable metadata mismatch: {object_key}")
            if row["state"] != state:
                return self._transition_connection(
                    connection, row, state, reason=reason, error_code=None
                )
            return row
        if replica_type not in {"local", "cloud"} or state not in self._TRANSITIONS:
            raise ValueError("archive replica state is invalid")
        now = utc_now()
        replica_id = new_id("archive-replica")
        values = self._state_values(
            state,
            now=now,
            previous=None,
            error_code=None,
        )
        state_hash = self._state_hash(
            subject_id,
            object_key,
            archive_object_kind(object_key),
            replica_type,
            provider_id,
            ciphertext_hash,
            byte_size,
            state,
            **values,
        )
        connection.execute(
            "INSERT INTO archive_object_replicas(replica_id, subject_id, object_key, "
            "object_kind, replica_type, provider_id, ciphertext_hash, byte_size, state, "
            "current_revision, verified_at, last_accessed_at, removed_at, restored_at, "
            "last_error_code, state_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                replica_id,
                subject_id,
                object_key,
                archive_object_kind(object_key),
                replica_type,
                provider_id,
                ciphertext_hash,
                byte_size,
                state,
                values["verified_at"],
                values["last_accessed_at"],
                values["removed_at"],
                values["restored_at"],
                values["last_error_code"],
                state_hash,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM archive_object_replicas WHERE replica_id = ?", (replica_id,)
        ).fetchone()
        self._insert_revision(connection, row, reason, now)
        return row

    def transition(
        self,
        replica_id: str,
        state: str,
        *,
        reason: str,
        error_code: str | None = None,
    ) -> Any:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM archive_object_replicas WHERE replica_id = ?", (replica_id,)
            ).fetchone()
            if row is None:
                raise IntegrityError(f"archive replica is missing: {replica_id}")
            return self._transition_connection(
                connection, row, state, reason=reason, error_code=error_code
            )

    def _transition_connection(
        self,
        connection: Any,
        row: Any,
        state: str,
        *,
        reason: str,
        error_code: str | None,
    ) -> Any:
        current = str(row["state"])
        if state not in self._TRANSITIONS.get(current, set()):
            raise IntegrityError(f"invalid archive replica transition: {current}->{state}")
        now = utc_now()
        values = self._state_values(state, now=now, previous=row, error_code=error_code)
        revision = int(row["current_revision"]) + 1
        state_hash = self._state_hash(
            str(row["subject_id"]),
            str(row["object_key"]),
            str(row["object_kind"]),
            str(row["replica_type"]),
            str(row["provider_id"]),
            str(row["ciphertext_hash"]),
            int(row["byte_size"]),
            state,
            **values,
        )
        connection.execute(
            "UPDATE archive_object_replicas SET state = ?, current_revision = ?, "
            "verified_at = ?, last_accessed_at = ?, removed_at = ?, restored_at = ?, "
            "last_error_code = ?, state_hash = ?, updated_at = ? WHERE replica_id = ?",
            (
                state,
                revision,
                values["verified_at"],
                values["last_accessed_at"],
                values["removed_at"],
                values["restored_at"],
                values["last_error_code"],
                state_hash,
                now,
                row["replica_id"],
            ),
        )
        updated = connection.execute(
            "SELECT * FROM archive_object_replicas WHERE replica_id = ?", (row["replica_id"],)
        ).fetchone()
        self._insert_revision(connection, updated, reason, now)
        return updated

    def find(
        self,
        subject_id: str,
        object_key: str,
        *,
        replica_type: str,
        provider_id: str | None = None,
    ) -> Any | None:
        query = (
            "SELECT * FROM archive_object_replicas WHERE subject_id = ? AND object_key = ? "
            "AND replica_type = ?"
        )
        parameters: tuple[Any, ...] = (subject_id, object_key, replica_type)
        if provider_id is not None:
            query += " AND provider_id = ?"
            parameters += (provider_id,)
        query += " ORDER BY updated_at DESC, replica_id DESC LIMIT 1"
        with self.database.connection() as connection:
            return connection.execute(query, parameters).fetchone()

    @staticmethod
    def _state_values(
        state: str, *, now: str, previous: Any | None, error_code: str | None
    ) -> dict[str, str | None]:
        prior: dict[str, str | None] = (
            {
                "verified_at": None,
                "last_accessed_at": None,
                "removed_at": None,
                "restored_at": None,
            }
            if previous is None
            else {
                "verified_at": previous["verified_at"],
                "last_accessed_at": previous["last_accessed_at"],
                "removed_at": previous["removed_at"],
                "restored_at": previous["restored_at"],
            }
        )
        if state in {"verified", "present"}:
            prior["verified_at"] = prior["verified_at"] or now
            prior["last_accessed_at"] = now
        if state == "absent":
            prior["removed_at"] = now
        if state == "present" and previous is not None and previous["state"] == "restoring":
            prior["restored_at"] = now
            prior["removed_at"] = None
        return {**prior, "last_error_code": error_code}

    @staticmethod
    def _state_hash(
        subject_id: str,
        object_key: str,
        object_kind: str,
        replica_type: str,
        provider_id: str,
        ciphertext_hash: str,
        byte_size: int,
        state: str,
        *,
        verified_at: str | None,
        last_accessed_at: str | None,
        removed_at: str | None,
        restored_at: str | None,
        last_error_code: str | None,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "object_key": object_key,
                "object_kind": object_kind,
                "replica_type": replica_type,
                "provider_id": provider_id,
                "ciphertext_hash": ciphertext_hash,
                "byte_size": byte_size,
                "state": state,
                "verified_at": verified_at,
                "last_accessed_at": last_accessed_at,
                "removed_at": removed_at,
                "restored_at": restored_at,
                "last_error_code": last_error_code,
            }
        )

    @classmethod
    def _insert_revision(cls, connection: Any, row: Any, reason: str, created_at: str) -> None:
        connection.execute(
            "INSERT INTO archive_object_replica_revisions(revision_id, replica_id, subject_id, "
            "revision_number, object_key, object_kind, replica_type, provider_id, "
            "ciphertext_hash, byte_size, state, verified_at, last_accessed_at, removed_at, "
            "restored_at, last_error_code, reason, state_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("archive-replica-rev"),
                row["replica_id"],
                row["subject_id"],
                row["current_revision"],
                row["object_key"],
                row["object_kind"],
                row["replica_type"],
                row["provider_id"],
                row["ciphertext_hash"],
                row["byte_size"],
                row["state"],
                row["verified_at"],
                row["last_accessed_at"],
                row["removed_at"],
                row["restored_at"],
                row["last_error_code"],
                reason,
                row["state_hash"],
                created_at,
            ),
        )


class ArchiveTransferQueue:
    """Durable upload queue with leased claims, CAS completion, and bounded retries."""

    def __init__(self, database: Database, subject_id: str, root: Path | str):
        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self.root = Path(root).expanduser().resolve()
        self.storage_key: str | None = None
        self.legacy_storage_path_is_unambiguous = False

    def enqueue(self, object_key: str, payload: bytes, *, storage_class: str = "cloud") -> str:
        if storage_class not in {"cold", "cloud"}:
            raise ValueError("invalid transfer storage class")
        key_parts = object_key.split("/")
        if (
            not object_key.strip()
            or object_key.startswith("/")
            or "\\" in object_key
            or any(part in {"", ".", ".."} for part in key_parts)
        ):
            raise ValueError("archive transfer object key must be a canonical relative POSIX path")
        # Queue staging is a filesystem write followed by its durable queue
        # row.  Keep both under the active runtime commit fence when called
        # from a leased background tick; otherwise a quarantine between the
        # two steps could leave an unowned payload with no recovery row.
        with current_commit_scope():
            queue_root = self._queue_root(create=True)
            payload_path = queue_root / object_key
            payload_path = payload_path.resolve()
            if queue_root not in payload_path.parents:
                raise ValueError("archive queue path escapes its root")
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            current = payload_path.parent
            while current != queue_root:
                if current.is_symlink():
                    raise ValueError("archive queue path contains a symbolic link")
                current = current.parent
            if payload_path.is_symlink():
                raise ValueError("archive queue target cannot be a symbolic link")
            now = utc_now()
            payload_hash = hashlib.sha256(payload).hexdigest()
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM archive_transfer_queue WHERE subject_id = ? AND object_key = ?",
                    (self.subject_id, object_key),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["storage_class"]) != storage_class
                        or int(existing["byte_size"]) != len(payload)
                        or str(existing["content_hash"]) != payload_hash
                    ):
                        raise IntegrityError(
                            "archive transfer object key cannot be rebound to different content"
                        )
                    transfer_id = str(existing["transfer_id"])
                    if existing["status"] == "uploading":
                        return transfer_id
                    payload_path = self._queued_payload_path(existing["payload_path"])
                else:
                    transfer_id = new_id("transfer")

                temporary = payload_path.with_name(f".{new_id('qwrite')}.tmp")
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    try:
                        view = memoryview(payload)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("archive queue write made no progress")
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.replace(temporary, payload_path)
                    LocalArchiveProvider._fsync_parent_directory(payload_path.parent)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise

                if existing is not None:
                    connection.execute(
                        "UPDATE archive_transfer_queue SET status = 'queued', attempts = 0, "
                        "next_attempt_at = ?, last_error = NULL, claim_token = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                        "WHERE transfer_id = ?",
                        (now, now, transfer_id),
                    )
                    return transfer_id
                connection.execute(
                    """INSERT INTO archive_transfer_queue(
                        transfer_id, subject_id, storage_class, object_key, payload_path,
                        byte_size, content_hash, status, attempts, next_attempt_at,
                        last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, NULL, ?, ?)
                    """,
                    (
                        transfer_id,
                        self.subject_id,
                        storage_class,
                        object_key,
                        str(payload_path),
                        len(payload),
                        payload_hash,
                        now,
                        now,
                        now,
                    ),
                )
            return transfer_id

    def drain(
        self,
        provider: ArchiveProvider,
        *,
        limit: int = 10,
        max_attempts: int = 5,
        lease_seconds: int = 900,
        worker_owner: str | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 86_400:
            raise ValueError("archive transfer lease must be between 1 and 86400 seconds")
        owner = worker_owner or new_id("archive-worker")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("archive transfer worker owner is required")
        if checkpoint is not None:
            checkpoint()
        provider_id = archive_provider_id(provider)
        now = utc_now()
        recovered = 0
        while recovered < ARCHIVE_EXPIRED_LEASE_RECOVERY_BUDGET:
            with self.database.transaction() as connection:
                transfer = connection.execute(
                    "SELECT transfer_id, object_key FROM archive_transfer_queue "
                    "WHERE subject_id = ? AND status = 'uploading' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                    "ORDER BY lease_expires_at, transfer_id LIMIT 1",
                    (self.subject_id, now),
                ).fetchone()
                if transfer is None:
                    break
                changed = connection.execute(
                    "UPDATE archive_transfer_queue SET status = 'failed', last_error = ?, "
                    "next_attempt_at = ?, claim_token = NULL, lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE transfer_id = ? "
                    "AND subject_id = ? AND status = 'uploading' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                    (
                        "expired_upload_lease_recovered",
                        now,
                        now,
                        transfer["transfer_id"],
                        self.subject_id,
                        now,
                    ),
                )
                if changed.rowcount != 1:
                    continue
                replica = connection.execute(
                    "SELECT * FROM archive_object_replicas WHERE subject_id = ? "
                    "AND object_key = ? AND replica_type = 'cloud' AND provider_id = ? "
                    "AND state = 'uploading' ORDER BY updated_at DESC, replica_id DESC LIMIT 1",
                    (self.subject_id, transfer["object_key"], provider_id),
                ).fetchone()
                if replica is not None:
                    replicas = ArchiveReplicaLedger(self.database)
                    replicas._transition_connection(
                        connection,
                        replica,
                        "unavailable",
                        reason="expired archive upload lease recovered",
                        error_code="expired_upload_lease_recovered",
                    )
            recovered += 1
        bounded_limit = max(1, min(limit, 100))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_transfer_queue WHERE subject_id = ? "
                "AND status IN ('queued','failed') AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, transfer_id LIMIT ?",
                (self.subject_id, now, bounded_limit),
            ).fetchmany(bounded_limit)
        result = {"uploaded": 0, "failed": 0, "dead": 0}
        replicas = ArchiveReplicaLedger(self.database)
        for candidate in rows:
            if checkpoint is not None:
                checkpoint()
            claim_now = utc_now()
            transfer_id = str(candidate["transfer_id"])
            claim_token = new_id("archive-claim")
            lease_expires_at = self._lease_expiry(claim_now, lease_seconds)
            replica_id: str | None = None
            with self.database.transaction() as connection:
                claimed = connection.execute(
                    "UPDATE archive_transfer_queue SET status = 'uploading', "
                    "attempts = attempts + 1, claim_token = ?, lease_owner = ?, "
                    "lease_expires_at = ?, updated_at = ? WHERE transfer_id = ? "
                    "AND subject_id = ? AND status IN ('queued','failed') "
                    "AND next_attempt_at <= ?",
                    (
                        claim_token,
                        owner,
                        lease_expires_at,
                        claim_now,
                        transfer_id,
                        self.subject_id,
                        claim_now,
                    ),
                )
                if claimed.rowcount != 1:
                    continue
                row = connection.execute(
                    "SELECT * FROM archive_transfer_queue WHERE transfer_id = ? "
                    "AND claim_token = ?",
                    (transfer_id, claim_token),
                ).fetchone()
                if row is None:
                    continue
                object_key = str(row["object_key"])
                attempts = int(row["attempts"])
                if object_key.startswith(("events/", "observations/")):
                    replica = replicas._ensure_connection(
                        connection,
                        self.subject_id,
                        object_key,
                        replica_type="cloud",
                        provider_id=provider_id,
                        ciphertext_hash=str(row["content_hash"]),
                        byte_size=int(row["byte_size"]),
                        state="uploading",
                        reason="archive upload claim acquired",
                    )
                    replica_id = str(replica["replica_id"])
            try:
                if checkpoint is not None:
                    checkpoint()
                payload_path = self._queued_payload_path(row["payload_path"])
                with payload_path.open("rb") as stream:
                    payload = stream.read(int(row["byte_size"]) + 1)
                if len(payload) != int(row["byte_size"]) or (
                    hashlib.sha256(payload).hexdigest() != row["content_hash"]
                ):
                    raise OSError("archive queue payload checksum mismatch")
                digest = provider.put(object_key, payload)
                if not self._renew_claim(
                    transfer_id,
                    claim_token,
                    owner,
                    lease_seconds=lease_seconds,
                ):
                    # The provider result is now deliberately unknown.  A new
                    # owner will reconcile the idempotent object key after the
                    # expired lease is recovered; this worker must not commit.
                    continue
                if checkpoint is not None:
                    checkpoint()
                if digest != row["content_hash"]:
                    raise OSError("archive provider checksum mismatch")
                restored = get_archive_payload(
                    provider,
                    object_key,
                    max_bytes=int(row["byte_size"]),
                )
                if len(restored) != int(row["byte_size"]) or (
                    hashlib.sha256(restored).hexdigest() != row["content_hash"]
                ):
                    raise IntegrityError("archive provider readback checksum mismatch")
            except Exception as error:
                if checkpoint is not None:
                    checkpoint()
                dead = attempts >= max(1, max_attempts)
                status = "dead" if dead else "failed"
                retry_at = (
                    datetime.fromisoformat(utc_now()) + timedelta(seconds=min(3_600, 2**attempts))
                ).isoformat(timespec="milliseconds")
                updated = False
                with self.database.transaction() as connection:
                    changed = connection.execute(
                        "UPDATE archive_transfer_queue SET status = ?, last_error = ?, "
                        "next_attempt_at = ?, claim_token = NULL, lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = ? WHERE transfer_id = ? "
                        "AND status = 'uploading' AND claim_token = ? AND lease_owner = ?",
                        (
                            status,
                            type(error).__name__,
                            retry_at,
                            utc_now(),
                            transfer_id,
                            claim_token,
                            owner,
                        ),
                    )
                    updated = changed.rowcount == 1
                    if updated and replica_id is not None:
                        replica = connection.execute(
                            "SELECT * FROM archive_object_replicas WHERE replica_id = ?",
                            (replica_id,),
                        ).fetchone()
                        replica_state = (
                            "missing"
                            if isinstance(error, FileNotFoundError)
                            else "corrupt"
                            if isinstance(error, (IntegrityError, PayloadLimitError))
                            else "unavailable"
                        )
                        replicas._transition_connection(
                            connection,
                            replica,
                            replica_state,
                            reason="archive upload verification failed",
                            error_code=type(error).__name__,
                        )
                if updated:
                    result[status] += 1
                continue
            committed = False
            with current_commit_scope():
                if checkpoint is not None:
                    checkpoint()
                with self.database.transaction() as connection:
                    completed_at = utc_now()
                    changed = connection.execute(
                        "UPDATE archive_transfer_queue SET status = 'uploaded', last_error = NULL, "
                        "claim_token = NULL, lease_owner = NULL, lease_expires_at = NULL, "
                        "updated_at = ? WHERE transfer_id = ? AND status = 'uploading' "
                        "AND claim_token = ? AND lease_owner = ? AND lease_expires_at > ?",
                        (
                            completed_at,
                            transfer_id,
                            claim_token,
                            owner,
                            completed_at,
                        ),
                    )
                    if changed.rowcount == 1:
                        connection.execute(
                            "UPDATE storage_archives SET status = 'verified', verified_at = ? "
                            "WHERE subject_id = ? AND object_key = ? AND content_hash = ?",
                            (
                                completed_at,
                                self.subject_id,
                                row["object_key"],
                                row["content_hash"],
                            ),
                        )
                        if replica_id is not None:
                            replica = connection.execute(
                                "SELECT * FROM archive_object_replicas WHERE replica_id = ?",
                                (replica_id,),
                            ).fetchone()
                            replicas._transition_connection(
                                connection,
                                replica,
                                "verified",
                                reason="archive cloud readback verified",
                                error_code=None,
                            )
                        committed = True
            if not committed:
                continue
            # A crash after the committed CAS can only leave a redundant queue
            # payload.  Uploaded rows are authoritative, so cleanup is safe
            # and repeatable on every subsequent drain.
            with suppress(OSError):
                payload_path.unlink(missing_ok=True)
            result["uploaded"] += 1
        self._cleanup_uploaded_payloads(limit=max(1, min(limit, 100)))
        return result

    @staticmethod
    def _lease_expiry(now: str, lease_seconds: int) -> str:
        return (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )

    def _renew_claim(
        self,
        transfer_id: str,
        claim_token: str,
        owner: str,
        *,
        lease_seconds: int,
    ) -> bool:
        now = utc_now()
        expires_at = self._lease_expiry(now, lease_seconds)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE archive_transfer_queue SET lease_expires_at = ?, updated_at = ? "
                "WHERE transfer_id = ? AND subject_id = ? AND status = 'uploading' "
                "AND claim_token = ? AND lease_owner = ? AND lease_expires_at > ?",
                (
                    expires_at,
                    now,
                    transfer_id,
                    self.subject_id,
                    claim_token,
                    owner,
                    now,
                ),
            )
        return changed.rowcount == 1

    def _cleanup_uploaded_payloads(self, *, limit: int) -> int:
        bounded_limit = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT transfer_id, payload_path FROM archive_transfer_queue WHERE subject_id = ? "
                "AND status = 'uploaded' ORDER BY updated_at, transfer_id LIMIT ?",
                (self.subject_id, bounded_limit),
            ).fetchmany(bounded_limit)
        removed = 0
        for row in rows:
            deleted = False
            try:
                path = self._queued_payload_path(row["payload_path"])
                # Keep the SQLite write lock while unlinking the local staging
                # file.  ``enqueue`` also updates the row under BEGIN IMMEDIATE;
                # this serialization prevents a fresh queued payload from being
                # written between the SELECT and unlink and then removed by a
                # stale cleanup pass.
                with self.database.transaction() as connection:
                    current = connection.execute(
                        "SELECT payload_path FROM archive_transfer_queue "
                        "WHERE transfer_id = ? AND subject_id = ? AND status = 'uploaded'",
                        (str(row["transfer_id"]), self.subject_id),
                    ).fetchone()
                    if current is None or str(current["payload_path"]) != str(row["payload_path"]):
                        continue
                    deleted = path.exists()
                    path.unlink(missing_ok=True)
            except (IntegrityError, OSError, ValueError):
                continue
            removed += int(deleted)
        return removed

    def gc_orphans(self, *, grace_seconds: int = 3_600, limit: int = 100) -> int:
        """Delete bounded, aged queue files that have no durable queue row."""
        try:
            queue_root = self._queue_root(create=False)
        except (FileNotFoundError, ValueError):
            return 0
        if not queue_root.is_dir():
            return 0
        referenced: set[Path] = set()
        reference_cursor: str | None = None
        reference_count = 0
        while True:
            with self.database.connection() as connection:
                if reference_cursor is None:
                    row = connection.execute(
                        "SELECT transfer_id, payload_path FROM archive_transfer_queue "
                        "WHERE subject_id = ? ORDER BY transfer_id LIMIT 1",
                        (self.subject_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT transfer_id, payload_path FROM archive_transfer_queue "
                        "WHERE subject_id = ? AND transfer_id > ? ORDER BY transfer_id LIMIT 1",
                        (self.subject_id, reference_cursor),
                    ).fetchone()
            if row is None:
                break
            reference_cursor = str(row["transfer_id"])
            reference_count += 1
            if reference_count > ARCHIVE_ORPHAN_REFERENCE_BUDGET:
                # A complete reference set is required before deleting any
                # orphan.  Preserve all files when the bounded read is not
                # sufficient to prove completeness.
                return 0
            try:
                referenced.add(self._queued_payload_path(row["payload_path"]).resolve())
            except (IntegrityError, OSError, ValueError):
                # A malformed durable path is an integrity finding.  Preserve
                # every file rather than guessing which one belongs to it.
                return 0

        cutoff = time.time() - max(0, grace_seconds)
        bounded_limit = max(1, min(limit, 1_000))
        scan_limit = max(100, bounded_limit * 20)
        stack: list[tuple[Path, bool]] = [(queue_root, False)]
        removed = examined = 0
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        while stack and removed < bounded_limit and examined < scan_limit:
            directory, closing = stack.pop()
            if closing:
                with suppress(OSError):
                    directory.rmdir()
                continue
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    if removed >= bounded_limit or examined >= scan_limit:
                        break
                    examined += 1
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(metadata.st_mode) or bool(
                        getattr(metadata, "st_file_attributes", 0) & reparse_flag
                    ):
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append((path, True))
                        stack.append((path, False))
                        continue
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_mtime > cutoff
                        or path.resolve() in referenced
                    ):
                        continue
                    with current_commit_scope():
                        try:
                            path.unlink()
                        except OSError:
                            continue
                    removed += 1
        return removed

    def _queued_payload_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError("archive queue payload path is invalid")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise IntegrityError("archive queue payload path is invalid")
        try:
            queue_root = self._queue_root(create=False)
        except ValueError as error:
            raise IntegrityError("archive queue subject directory is invalid") from error
        legacy_root = Path(os.path.abspath(self.root / "archive_queue" / self.subject_id))
        try:
            relative = candidate.relative_to(queue_root)
        except ValueError:
            try:
                relative = candidate.relative_to(legacy_root)
            except ValueError as error:
                raise IntegrityError("archive queue payload path escapes its root") from error
            candidate = queue_root / relative
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise IntegrityError("archive queue payload path escapes its root")
        resolved = candidate.resolve()
        if resolved == queue_root or queue_root not in resolved.parents:
            raise IntegrityError("archive queue payload path escapes its root")
        current = candidate.parent
        while current != queue_root:
            if current.parent == current:
                raise IntegrityError("archive queue payload path escapes its root")
            if current.is_symlink():
                raise IntegrityError("archive queue payload path contains a symbolic link")
            current = current.parent
        if candidate.is_symlink():
            raise IntegrityError("archive queue payload path cannot be a symbolic link")
        return candidate

    def _queue_root(self, *, create: bool) -> Path:
        if self.storage_key is None:
            identities = IdentityStore(self.database)
            self.storage_key = identities.storage_key(self.subject_id)
            self.legacy_storage_path_is_unambiguous = identities.legacy_storage_path_is_unambiguous(
                self.subject_id
            )
        located = SubjectStorageDirectory.locate(
            self.root / "archive_queue",
            self.storage_key,
            legacy_subject_id=self.subject_id,
            legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
            create=create,
        )
        if located is None:
            raise ValueError("archive queue subject directory is missing")
        return located


class ArchiveReplicaResolver:
    """Restore a missing local encrypted object from a fully verified cloud replica."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        cloud_provider: ArchiveProvider | None = None,
    ):
        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self._cloud_provider = cloud_provider
        self.replicas = ArchiveReplicaLedger(database)

    def cloud_provider(self) -> ArchiveProvider:
        if self._cloud_provider is not None:
            return self._cloud_provider
        if not os.getenv("NOYRA_ARCHIVE_S3_BUCKET", "").strip():
            raise ArchiveUnavailableError("cloud archive provider is not configured")
        try:
            self._cloud_provider = S3ArchiveProvider.from_env()
        except Exception as error:
            raise ArchiveUnavailableError("cloud archive provider is unavailable") from error
        return self._cloud_provider

    def restore_local(
        self,
        object_key: str,
        local_provider: LocalArchiveProvider,
        *,
        cache: bool = True,
        expected_plaintext_hash: str | None = None,
        max_plaintext_bytes: int | None = None,
    ) -> bytes:
        with self.database.connection() as connection:
            manifest = connection.execute(
                "SELECT * FROM storage_archives WHERE subject_id = ? AND object_key = ? "
                "AND storage_class = 'cloud' AND status = 'verified'",
                (self.subject_id, object_key),
            ).fetchone()
        if manifest is None:
            raise IntegrityError("verified cloud archive metadata is missing")
        provider = self.cloud_provider()
        provider_id = archive_provider_id(provider)
        with self.database.connection() as connection:
            configured_replica = connection.execute(
                "SELECT * FROM archive_object_replicas WHERE subject_id = ? AND object_key = ? "
                "AND replica_type = 'cloud' AND provider_id = ? ORDER BY updated_at DESC LIMIT 1",
                (self.subject_id, object_key, provider_id),
            ).fetchone()
            other_verified = connection.execute(
                "SELECT 1 FROM archive_object_replicas WHERE subject_id = ? AND object_key = ? "
                "AND replica_type = 'cloud' AND state = 'verified' AND provider_id != ? LIMIT 1",
                (self.subject_id, object_key, provider_id),
            ).fetchone()
        if configured_replica is None and other_verified is not None:
            raise ArchiveUnavailableError(
                "cloud archive provider does not match the verified replica"
            )
        if configured_replica is not None and (
            configured_replica["verified_at"] is None
            or configured_replica["state"] not in {"verified", "unavailable", "missing", "corrupt"}
        ):
            raise ArchiveUnavailableError("configured cloud archive replica is not verified")
        expected_size = int(manifest["byte_size"])
        expected_hash = str(manifest["content_hash"])
        stored_limit = local_provider._stored_limit(max_plaintext_bytes)
        if stored_limit is not None and expected_size > stored_limit:
            raise PayloadLimitError("cloud archive object exceeds the configured read limit")
        if not cache:
            try:
                stored = get_archive_payload(provider, object_key, max_bytes=expected_size)
            except FileNotFoundError as error:
                raise IntegrityError("verified cloud archive object is missing") from error
            except (PermissionError, OSError) as error:
                raise ArchiveUnavailableError("cloud archive object is unavailable") from error
            return self._verify_restored_payload(
                object_key,
                local_provider,
                stored,
                expected_size=expected_size,
                expected_ciphertext_hash=expected_hash,
                expected_plaintext_hash=expected_plaintext_hash,
                max_plaintext_bytes=max_plaintext_bytes,
            )
        local = self.replicas.ensure(
            self.subject_id,
            object_key,
            replica_type="local",
            provider_id="local-cold-v1",
            ciphertext_hash=expected_hash,
            byte_size=expected_size,
            state="absent",
            reason="local archive object is absent",
        )
        local = self.replicas.transition(
            str(local["replica_id"]),
            "restoring",
            reason="cloud read-through restore started",
        )
        try:
            stored = get_archive_payload(provider, object_key, max_bytes=expected_size)
            plaintext = self._verify_restored_payload(
                object_key,
                local_provider,
                stored,
                expected_size=expected_size,
                expected_ciphertext_hash=expected_hash,
                expected_plaintext_hash=expected_plaintext_hash,
                max_plaintext_bytes=max_plaintext_bytes,
            )
        except FileNotFoundError as error:
            self.replicas.transition(
                str(local["replica_id"]),
                "missing",
                reason="verified cloud replica is missing",
                error_code=type(error).__name__,
            )
            self._mark_cloud_failure(configured_replica, "missing", error)
            raise IntegrityError("verified cloud archive object is missing") from error
        except (PermissionError, OSError) as error:
            self.replicas.transition(
                str(local["replica_id"]),
                "unavailable",
                reason="cloud read-through restore is unavailable",
                error_code=type(error).__name__,
            )
            self._mark_cloud_failure(configured_replica, "unavailable", error)
            raise ArchiveUnavailableError("cloud archive object is unavailable") from error
        except (ArchiveAuthenticationError, IntegrityError, PayloadLimitError) as error:
            self.replicas.transition(
                str(local["replica_id"]),
                "corrupt",
                reason="cloud read-through restore failed integrity verification",
                error_code=type(error).__name__,
            )
            self._mark_cloud_failure(configured_replica, "corrupt", error)
            raise
        except Exception as error:
            self.replicas.transition(
                str(local["replica_id"]),
                "unavailable",
                reason="cloud read-through restore provider failed",
                error_code=type(error).__name__,
            )
            self._mark_cloud_failure(configured_replica, "unavailable", error)
            raise ArchiveUnavailableError("cloud archive object is unavailable") from error
        try:
            # Cache publication and its replica state are one fenced commit.
            # If a pause/quarantine wins before this scope is entered, no
            # stale worker can materialize a local object after the boundary.
            with current_commit_scope():
                local_provider.put_stored(object_key, stored)
                self.replicas.transition(
                    str(local["replica_id"]),
                    "present",
                    reason="cloud read-through restore completed",
                )
        except OSError as error:
            self.replicas.transition(
                str(local["replica_id"]),
                "unavailable",
                reason="cloud read-through local cache write failed",
                error_code=type(error).__name__,
            )
            raise ArchiveUnavailableError("local archive cache is unavailable") from error
        self.replicas.ensure(
            self.subject_id,
            object_key,
            replica_type="cloud",
            provider_id=provider_id,
            ciphertext_hash=expected_hash,
            byte_size=expected_size,
            state="verified",
            reason="cloud replica verified during read-through",
        )
        return plaintext

    @staticmethod
    def _verify_restored_payload(
        object_key: str,
        local_provider: LocalArchiveProvider,
        stored: bytes,
        *,
        expected_size: int,
        expected_ciphertext_hash: str,
        expected_plaintext_hash: str | None,
        max_plaintext_bytes: int | None,
    ) -> bytes:
        if (
            len(stored) != expected_size
            or hashlib.sha256(stored).hexdigest() != expected_ciphertext_hash
        ):
            raise IntegrityError("cloud archive read-through checksum mismatch")
        plaintext = local_provider.decrypt_stored(object_key, stored)
        if max_plaintext_bytes is not None and len(plaintext) > max_plaintext_bytes:
            raise PayloadLimitError("archive object exceeds the configured read limit")
        if (
            expected_plaintext_hash is not None
            and hashlib.sha256(plaintext).hexdigest() != expected_plaintext_hash
        ):
            raise IntegrityError("cloud archive read-through plaintext hash mismatch")
        return plaintext

    def _mark_cloud_failure(self, replica: Any | None, state: str, error: Exception) -> None:
        if replica is None:
            return
        with suppress(IntegrityError):
            self.replicas.transition(
                str(replica["replica_id"]),
                state,
                reason="cloud archive read-through verification failed",
                error_code=type(error).__name__,
            )


class ArchiveRecovery:
    """Restore a verified object atomically into an explicitly bounded root."""

    @staticmethod
    def restore(
        provider: ArchiveProvider,
        object_key: str,
        target_root: Path | str,
        relative_path: str,
        *,
        expected_hash: str,
    ) -> Path:
        root = Path(target_root).expanduser().resolve()
        target = (root / relative_path).resolve()
        if target == root or root not in target.parents:
            raise ValueError("archive restore path escapes target root")
        payload = provider.get(object_key)
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise OSError("restored archive checksum mismatch")
        current = target.parent
        while current != root:
            if current.is_symlink():
                raise ValueError("archive restore path contains a symbolic link")
            current = current.parent
        if target.is_symlink():
            raise ValueError("archive restore target cannot be a symbolic link")
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.parent
        while current != root:
            if current.is_symlink():
                raise ValueError("archive restore path contains a symbolic link")
            current = current.parent
        if target.is_symlink():
            raise ValueError("archive restore target cannot be a symbolic link")
        temporary = target.with_name(f".{new_id('arestore')}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("archive restore write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            LocalArchiveProvider._fsync_parent_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target


class CloudArchiveCoordinator:
    """Copy compacted snapshots to optional cloud storage without blocking local runtime."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        staging_root: Path | str,
        provider: ArchiveProvider | None = None,
        local_archive_root: Path | str | None = None,
    ):
        from .storage import ArchiveStore

        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self.provider = provider
        self.queue = ArchiveTransferQueue(database, subject_id, staging_root)
        self.archives = ArchiveStore(database)
        self.replicas = ArchiveReplicaLedger(database)
        self.local_archive_root = (
            None
            if local_archive_root is None
            else resolve_subject_archive_root(
                database,
                self.subject_id,
                local_archive_root,
                create=False,
            )
        )
        self.keyring = ArchiveKeyring.from_env() if ArchiveKeyring.configured() else None

    def tick(
        self,
        *,
        garbage_collect_local: bool = False,
        allow_staging: bool = True,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        """Run one bounded cloud maintenance pass.

        ``allow_staging=False`` preserves pressure-relieving work on payloads
        that are already queued while preventing this tick from copying any
        additional snapshots or local archive segments into the staging area.
        """
        orphaned = 0
        if checkpoint is not None:
            checkpoint()
        if self.keyring is not None and allow_staging:
            self.keyring.record_revision(self.database, self.subject_id)
            if checkpoint is not None:
                checkpoint()
        if garbage_collect_local:
            orphaned = self.queue.gc_orphans(limit=4)
            if checkpoint is not None:
                checkpoint()
        self._recover_local_transitions()
        if checkpoint is not None:
            checkpoint()
        if self.provider is None:
            return {
                "uploaded": 0,
                "failed": 0,
                "dead": 0,
                "garbage_collected": orphaned,
            }
        readiness = getattr(self.provider, "readiness", None)
        if callable(readiness):
            status = readiness()
            if not isinstance(status, dict) or not bool(status.get("ready")):
                # Do not burn durable queue attempts while a provider circuit
                # is open.  After cooldown, readiness performs the single
                # half-open probe and a successful tick resumes normal work.
                return {
                    "uploaded": 0,
                    "failed": 0,
                    "dead": 0,
                    "garbage_collected": orphaned,
                }
        if allow_staging:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """SELECT s.* FROM snapshot_archives s
                       LEFT JOIN storage_archives a
                         ON a.subject_id = s.subject_id
                        AND a.object_key = 'snapshots/' || s.archive_id || '.zlib'
                       WHERE s.subject_id = ?
                         AND (a.archive_id IS NULL OR a.status != 'verified')
                       ORDER BY s.created_at LIMIT 4""",
                    (self.subject_id,),
                ).fetchmany(4)
            for row in rows:
                if checkpoint is not None:
                    checkpoint()
                object_key = f"snapshots/{row['archive_id']}.zlib"
                payload = bytes(row["compressed_payload"])
                with self.database.connection() as connection:
                    transfer = connection.execute(
                        "SELECT status FROM archive_transfer_queue "
                        "WHERE subject_id = ? AND object_key = ?",
                        (self.subject_id, object_key),
                    ).fetchone()
                    archive = connection.execute(
                        "SELECT archive_id FROM storage_archives "
                        "WHERE subject_id = ? AND object_key = ?",
                        (self.subject_id, object_key),
                    ).fetchone()
                if archive is None:
                    self.archives.register(
                        self.subject_id,
                        storage_class="cloud",
                        object_key=object_key,
                        byte_size=len(payload),
                        payload=payload,
                    )
                if transfer is None:
                    self.queue.enqueue(object_key, payload)
            self._queue_event_segments()
            self._queue_observation_segments()
        result = self.queue.drain(self.provider, limit=4, checkpoint=checkpoint)
        result["garbage_collected"] = orphaned + (
            self._garbage_collect_local(limit=4) if garbage_collect_local else 0
        )
        return result

    def _queue_event_segments(self) -> None:
        if self.local_archive_root is None or self.provider is None:
            return
        provider_id = archive_provider_id(self.provider)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT s.* FROM event_payload_segments s
                   WHERE s.subject_id = ? AND NOT EXISTS (
                       SELECT 1 FROM archive_object_replicas r
                       WHERE r.subject_id = s.subject_id AND r.object_key = s.object_key
                         AND r.replica_type = 'cloud' AND r.provider_id = ?
                         AND r.state = 'verified'
                    ) ORDER BY s.created_at LIMIT 4""",
                (self.subject_id, provider_id),
            ).fetchmany(4)
        root = self.local_archive_root
        for row in rows:
            object_key = str(row["object_key"])
            payload_path = (root / object_key).resolve()
            if root not in payload_path.parents or not payload_path.is_file():
                continue
            size = payload_path.stat().st_size
            with payload_path.open("rb") as stream:
                payload = stream.read(size + 1)
            if len(payload) != size:
                raise IntegrityError("local event archive size changed during cloud staging")
            ciphertext_hash = hashlib.sha256(payload).hexdigest()
            self.replicas.ensure(
                self.subject_id,
                object_key,
                replica_type="local",
                provider_id="local-cold-v1",
                ciphertext_hash=ciphertext_hash,
                byte_size=size,
                state="present",
                reason="local event archive discovered",
            )
            with self.database.connection() as connection:
                archive = connection.execute(
                    "SELECT archive_id FROM storage_archives "
                    "WHERE subject_id = ? AND object_key = ?",
                    (self.subject_id, object_key),
                ).fetchone()
                transfer = connection.execute(
                    "SELECT status FROM archive_transfer_queue "
                    "WHERE subject_id = ? AND object_key = ?",
                    (self.subject_id, object_key),
                ).fetchone()
            if archive is None:
                self.archives.register(
                    self.subject_id,
                    storage_class="cloud",
                    object_key=object_key,
                    byte_size=len(payload),
                    payload=payload,
                )
            if transfer is None or transfer["status"] not in {"queued", "uploading"}:
                self.queue.enqueue(object_key, payload)

    def _queue_observation_segments(self) -> None:
        if self.local_archive_root is None or self.provider is None:
            return
        provider_id = archive_provider_id(self.provider)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT s.* FROM observation_content_segments s
                   WHERE s.subject_id = ? AND NOT EXISTS (
                       SELECT 1 FROM archive_object_replicas r
                       WHERE r.subject_id = s.subject_id AND r.object_key = s.object_key
                         AND r.replica_type = 'cloud' AND r.provider_id = ?
                         AND r.state = 'verified'
                    ) ORDER BY s.created_at LIMIT 4""",
                (self.subject_id, provider_id),
            ).fetchmany(4)
        root = self.local_archive_root
        for row in rows:
            object_key = str(row["object_key"])
            payload_path = (root / object_key).resolve()
            if root not in payload_path.parents or not payload_path.is_file():
                continue
            size = payload_path.stat().st_size
            with payload_path.open("rb") as stream:
                payload = stream.read(size + 1)
            if len(payload) != size:
                raise IntegrityError("local observation archive size changed during cloud staging")
            ciphertext_hash = hashlib.sha256(payload).hexdigest()
            self.replicas.ensure(
                self.subject_id,
                object_key,
                replica_type="local",
                provider_id="local-cold-v1",
                ciphertext_hash=ciphertext_hash,
                byte_size=size,
                state="present",
                reason="local observation archive discovered",
            )
            with self.database.connection() as connection:
                archive = connection.execute(
                    "SELECT archive_id FROM storage_archives "
                    "WHERE subject_id = ? AND object_key = ?",
                    (self.subject_id, object_key),
                ).fetchone()
                transfer = connection.execute(
                    "SELECT status FROM archive_transfer_queue "
                    "WHERE subject_id = ? AND object_key = ?",
                    (self.subject_id, object_key),
                ).fetchone()
            if archive is None:
                self.archives.register(
                    self.subject_id,
                    storage_class="cloud",
                    object_key=object_key,
                    byte_size=len(payload),
                    payload=payload,
                )
            if transfer is None or transfer["status"] not in {"queued", "uploading"}:
                self.queue.enqueue(object_key, payload)

    def _recover_local_transitions(self) -> None:
        if self.local_archive_root is None:
            return
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_object_replicas WHERE subject_id = ? "
                "AND replica_type = 'local' AND state IN ('gc_pending', 'restoring') "
                "ORDER BY updated_at, replica_id LIMIT 100",
                (self.subject_id,),
            ).fetchmany(100)
        for row in rows:
            path = (self.local_archive_root / str(row["object_key"])).resolve()
            state = "absent"
            error_code = None
            if self.local_archive_root not in path.parents or path.is_symlink():
                state = "corrupt"
                error_code = "invalid_local_path"
            elif path.is_file():
                try:
                    with path.open("rb") as stream:
                        payload = stream.read(int(row["byte_size"]) + 1)
                    if (
                        len(payload) == int(row["byte_size"])
                        and hashlib.sha256(payload).hexdigest() == row["ciphertext_hash"]
                    ):
                        state = "present"
                    else:
                        state = "corrupt"
                        error_code = "ciphertext_mismatch"
                except OSError as error:
                    state = "unavailable"
                    error_code = type(error).__name__
            self.replicas.transition(
                str(row["replica_id"]),
                state,
                reason="archive replica transition recovered after interruption",
                error_code=error_code,
            )

    def _garbage_collect_local(self, *, limit: int) -> int:
        if self.local_archive_root is None or self.provider is None or self.keyring is None:
            return 0
        provider_id = archive_provider_id(self.provider)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT local.*, cloud.replica_id AS cloud_replica_id "
                "FROM archive_object_replicas local "
                "JOIN archive_object_replicas cloud ON cloud.subject_id = local.subject_id "
                "AND cloud.object_key = local.object_key AND cloud.replica_type = 'cloud' "
                "AND cloud.provider_id = ? AND cloud.state = 'verified' "
                "WHERE local.subject_id = ? AND local.replica_type = 'local' "
                "AND local.state = 'present' ORDER BY local.updated_at, local.replica_id LIMIT ?",
                (provider_id, self.subject_id, max(1, min(limit, 100))),
            ).fetchmany(max(1, min(limit, 100)))
        removed = 0
        for row in rows:
            object_key = str(row["object_key"])
            pending = self.replicas.transition(
                str(row["replica_id"]),
                "gc_pending",
                reason="local archive garbage collection started",
            )
            try:
                local_provider, expected_plaintext_hash = self._segment_provider(object_key)
            except ArchiveKeyUnavailableError as error:
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive garbage collection awaits its encryption key",
                    error_code=type(error).__name__,
                )
                continue
            expected_ciphertext_hash = str(row["ciphertext_hash"])
            try:
                local_stored = local_provider.get_stored(
                    object_key, max_bytes=int(row["byte_size"])
                )
            except FileNotFoundError as error:
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "missing",
                    reason="local archive disappeared before garbage collection",
                    error_code=type(error).__name__,
                )
                continue
            except OSError as error:
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive garbage collection could not read the local object",
                    error_code=type(error).__name__,
                )
                continue
            if (
                len(local_stored) != int(row["byte_size"])
                or hashlib.sha256(local_stored).hexdigest() != expected_ciphertext_hash
            ):
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "corrupt",
                    reason="local archive failed verification before garbage collection",
                    error_code="ciphertext_mismatch",
                )
                raise IntegrityError("local archive ciphertext mismatch before garbage collection")
            try:
                remote_stored = get_archive_payload(
                    self.provider, object_key, max_bytes=int(row["byte_size"])
                )
            except FileNotFoundError as error:
                self._mark_cloud_replica_failure(row, "missing", error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive retained because the cloud replica is missing",
                )
                continue
            except OSError as error:
                self._mark_cloud_replica_failure(row, "unavailable", error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive retained because the cloud replica is unavailable",
                    error_code=type(error).__name__,
                )
                continue
            except (IntegrityError, PayloadLimitError) as error:
                self._mark_cloud_replica_failure(row, "corrupt", error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive retained because cloud verification failed",
                    error_code=type(error).__name__,
                )
                continue
            except Exception as error:
                self._mark_cloud_replica_failure(row, "unavailable", error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive retained because the cloud provider failed",
                    error_code=type(error).__name__,
                )
                continue
            if (
                len(remote_stored) != int(row["byte_size"])
                or remote_stored != local_stored
                or hashlib.sha256(remote_stored).hexdigest() != expected_ciphertext_hash
            ):
                mismatch_error = IntegrityError("cloud archive replica ciphertext mismatch")
                self._mark_cloud_replica_failure(row, "corrupt", mismatch_error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive retained because the cloud replica is corrupt",
                    error_code=type(mismatch_error).__name__,
                )
                continue
            try:
                plaintext = local_provider.decrypt_stored(object_key, remote_stored)
                if hashlib.sha256(plaintext).hexdigest() != expected_plaintext_hash:
                    raise IntegrityError("archive replica plaintext hash mismatch before local GC")
            except Exception as error:
                self._mark_cloud_replica_failure(row, "corrupt", error)
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "corrupt",
                    reason="local archive plaintext failed verification before garbage collection",
                    error_code=type(error).__name__,
                )
                raise
            try:
                with current_commit_scope():
                    local_provider.delete(object_key)
                    self.replicas.transition(
                        str(pending["replica_id"]),
                        "absent",
                        reason="local archive garbage collection completed",
                    )
            except OSError as error:
                self.replicas.transition(
                    str(pending["replica_id"]),
                    "present",
                    reason="local archive garbage collection delete was deferred",
                    error_code=type(error).__name__,
                )
                continue
            removed += 1
        return removed

    def _mark_cloud_replica_failure(self, row: Any, state: str, error: Exception) -> None:
        with suppress(IntegrityError):
            self.replicas.transition(
                str(row["cloud_replica_id"]),
                state,
                reason="cloud archive verification failed before local garbage collection",
                error_code=type(error).__name__,
            )

    def _segment_provider(self, object_key: str) -> tuple[LocalArchiveProvider, str]:
        assert self.local_archive_root is not None
        assert self.keyring is not None
        table = (
            "event_payload_segments"
            if object_key.startswith("events/")
            else "observation_content_segments"
        )
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT compressed_hash, encryption_key_id, encryption_key_fingerprint "
                f"FROM {table} WHERE subject_id = ? AND object_key = ?",
                (self.subject_id, object_key),
            ).fetchone()
        if row is None:
            raise IntegrityError("archive segment metadata is missing")
        provider = self.keyring.provider(
            self.local_archive_root,
            key_id=row["encryption_key_id"],
            fingerprint=row["encryption_key_fingerprint"],
            create_root=False,
        )
        return provider, str(row["compressed_hash"])


@dataclass(frozen=True)
class StorageQuota:
    subject_bytes: int = 2_000_000_000
    training_bytes: int = 5_000_000_000
    workspace_bytes: int = 20_000_000_000
    warning_ratio: float = 0.8

    def __post_init__(self) -> None:
        if min(self.subject_bytes, self.training_bytes, self.workspace_bytes) < 1:
            raise ValueError("storage quotas must be positive")
        if not 0 < self.warning_ratio < 1:
            raise ValueError("warning ratio must be between zero and one")


@dataclass(frozen=True)
class StorageUsage:
    subject_bytes: int
    training_bytes: int
    workspace_bytes: int
    free_bytes: int

    def over_quota(self, quota: StorageQuota) -> tuple[str, ...]:
        over: list[str] = []
        if self.subject_bytes > quota.subject_bytes:
            over.append("subject")
        if self.training_bytes > quota.training_bytes:
            over.append("training")
        if self.workspace_bytes > quota.workspace_bytes:
            over.append("workspace")
        return tuple(over)

    def warnings(self, quota: StorageQuota) -> tuple[str, ...]:
        warnings: list[str] = []
        for name, value, limit in (
            ("subject", self.subject_bytes, quota.subject_bytes),
            ("training", self.training_bytes, quota.training_bytes),
            ("workspace", self.workspace_bytes, quota.workspace_bytes),
        ):
            if value >= limit * quota.warning_ratio:
                warnings.append(name)
        return tuple(warnings)


class StorageUsageScanner:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    def scan(self) -> StorageUsage:
        database_paths = (
            self.root / "noyra.sqlite3",
            self.root / "noyra.sqlite3-wal",
            self.root / "noyra.sqlite3-shm",
        )
        database_bytes = sum(self._file_size(path) for path in database_paths)
        # Long-running exports use same-volume SQLite backup images.  Include
        # both active and abandoned images in quota accounting so a crash
        # cannot hide full-database copies from storage pressure handling.
        database_bytes += sum(
            self._file_size(path) for path in self.root.glob(".noyra.sqlite3.snapshot_*.sqlite*")
        )
        return StorageUsage(
            subject_bytes=(
                self._size(self.root / "subject")
                + self._size(self.root / "exports")
                + self._size(self.root / "secrets")
                + database_bytes
            ),
            training_bytes=self._size(self.root / "training_raw"),
            workspace_bytes=self._size(self.root / "workspace"),
            free_bytes=shutil.disk_usage(self.root).free,
        )

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            if path.is_symlink() or not path.is_file():
                return 0
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _size(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            if path.is_symlink():
                return 0
            if path.is_file():
                return path.stat().st_size
        except OSError:
            return 0
        total = 0
        try:
            entries = path.rglob("*")
            for item in entries:
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                    total += item.stat().st_size
                except OSError:
                    continue
        except OSError:
            return total
        return total
