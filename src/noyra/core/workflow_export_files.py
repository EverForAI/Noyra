"""Private encrypted workflow objects with explicit local durability observation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from .archive import ArchiveKeyring, LocalArchiveProvider, resolve_subject_archive_root
from .at_rest import (
    _effective_uid,
    _windows_permission_audit,
    validate_private_file,
    validate_private_root,
)
from .database import Database
from .errors import IntegrityError
from .workflow_bundle import MAX_SEGMENTS, require

NAME = re.compile(r"(?:manifest|segment-[0-9]{6})\.enc\Z")
PUBLICATION = re.compile(r"wexport_[a-f0-9]{64}\Z")


class WorkflowPublicationUnsupportedError(OSError):
    """The platform cannot confirm new workflow directory publication durability."""


class WorkflowArtifactStore:
    publication_pattern = PUBLICATION
    storage_directory = "workflow-exports"

    @staticmethod
    def creation_supported() -> bool:
        # This is admission capability, not a promise that a particular fsync succeeds.
        return os.name == "posix"

    @classmethod
    def creation_capability(cls) -> dict[str, Any]:
        supported = cls.creation_supported()
        return {
            "supported": supported,
            "reason_code": None if supported else "platform_directory_durability_unavailable",
            "scope": "new_or_prepared_workflow_publication",
            "history_read_available": True,
        }

    @classmethod
    def require_creation_supported(cls) -> None:
        if not cls.creation_supported():
            raise WorkflowPublicationUnsupportedError("platform_directory_durability_unavailable")

    def __init__(
        self,
        database: Database,
        subject: str,
        publication: str,
        ring: ArchiveKeyring,
        key_id: str,
        fingerprint: str,
        *,
        create: bool,
    ):
        if not self.publication_pattern.fullmatch(publication):
            raise ValueError("invalid workflow publication identity")
        cold = resolve_subject_archive_root(
            database,
            subject,
            database.path.parent / "subject" / "cold",
            create=create,
        )
        configured = cold / self.storage_directory / publication
        self.root = validate_private_root(
            configured,
            create=create and not configured.exists(),
            expected_device=database.path.parent.stat().st_dev,
            label="workflow export storage",
        )
        if os.name == "nt" and not str(self.root).startswith("\\\\?\\"):
            # The source data directory may already be deep in a user profile.
            # Native extended paths keep atomic temporary filenames usable too.
            raw_root = str(self.root)
            self.root = Path(
                "\\\\?\\UNC\\" + raw_root[2:]
                if raw_root.startswith("\\\\")
                else "\\\\?\\" + raw_root
            )
        self.publication = publication
        self.data_root = database.path.parent
        self.provider = ring.provider(
            self.root, key_id=key_id, fingerprint=fingerprint, create_root=False
        )
        self.permissions()

    def permissions(self) -> None:
        validate_private_root(self.root, label="workflow export storage")
        if os.name == "nt":
            ready, _detail = _windows_permission_audit(self.root, max_entries=MAX_SEGMENTS + 4)
            require(ready, "storage ACL is unsafe or unavailable")
        else:
            metadata = self.root.stat()
            require(
                metadata.st_uid == _effective_uid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0,
                "storage permissions are unsafe",
            )

    def _file(self, name: str, *, missing: bool) -> tuple[str, Path]:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise IntegrityError("workflow export object name is invalid")
        reference = self.publication + "_" + name
        path = validate_private_file(self.root, reference, allow_missing=missing)
        if path.exists():
            metadata = path.stat()
            require(metadata.st_nlink == 1, "object is hard linked")
            if os.name != "nt":
                require(
                    metadata.st_uid == _effective_uid()
                    and stat.S_IMODE(metadata.st_mode) & 0o077 == 0,
                    "object permissions are unsafe",
                )
        return reference, path

    def put(self, name: str, value: bytes) -> dict[str, Any]:
        reference, _ = self._file(name, missing=True)
        self.provider.put(reference, value)
        self._file(name, missing=False)
        size, digest = self.provider.stored_metadata(reference, max_bytes=len(value) + 37)
        return {
            "name": name,
            "stored_bytes": size,
            "stored_hash": digest,
            "plaintext_hash": hashlib.sha256(value).hexdigest(),
        }

    def get(self, entry: dict[str, Any], *, maximum: int) -> bytes:
        require(
            isinstance(entry, dict)
            and set(entry)
            in (
                {"name", "stored_bytes", "stored_hash", "plaintext_hash"},
                {
                    "name",
                    "stored_bytes",
                    "stored_hash",
                    "plaintext_hash",
                    "records",
                    "first",
                    "last",
                },
            ),
            "object descriptor fields are invalid",
        )
        size = entry["stored_bytes"]
        require(type(size) is int and 37 <= size <= maximum + 37, "object size exceeds budget")
        reference, _ = self._file(entry["name"], missing=False)
        stored = self.provider.get_stored(reference, max_bytes=size)
        require(
            len(stored) == size and hashlib.sha256(stored).hexdigest() == entry["stored_hash"],
            "stored object digest differs",
        )
        plaintext = self.provider.decrypt_stored(reference, stored)
        require(
            hashlib.sha256(plaintext).hexdigest() == entry["plaintext_hash"],
            "plaintext digest differs",
        )
        return plaintext

    def flush(self) -> bool:
        """Only POSIX directory fsync currently confirms this publication contract."""
        self.permissions()
        if os.name == "nt":
            return False
        current = self.root
        while True:
            LocalArchiveProvider._fsync_parent_directory(current)
            if current == self.data_root:
                return True
            require(self.data_root in current.parents, "storage directory escaped data root")
            current = current.parent
