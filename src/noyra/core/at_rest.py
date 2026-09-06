"""Fail-closed at-rest controls and encrypted offline backups.

The hot SQLite database is intentionally left in SQLite's native format.  A
production runtime therefore starts only when the data root is on a verified
encrypted volume and its database/secret paths are private to the service
account.  Backups use a separate versioned keyring and chunked AES-256-GCM so
they remain encrypted after leaving that volume.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import IntegrityError, RuntimeOwnershipError
from .locking import ProcessLock
from .types import canonical_json, utc_now

AT_REST_THREAT_MODEL = "noyra-at-rest/offline-media-v1"
BACKUP_KEYRING_FORMAT = "noyra-backup-keyring/v1"
BACKUP_FORMAT = "noyra-encrypted-backup/v1"
BACKUP_MANIFEST_FORMAT = "noyra-backup-manifest/v1"
VOLUME_ATTESTATION_FORMAT = "noyra-volume-attestation/v1"

_BACKUP_MAGIC = b"NOYRA-BACKUP\x01\n"
_HEADER_LENGTH = struct.Struct(">I")
_RECORD_HEADER = struct.Struct(">BQI")
_DATA_RECORD = 1
_END_RECORD = 2
_MAX_HEADER_BYTES = 64 * 1024
_MAX_KEYRING_BYTES = 1024 * 1024
_MAX_KEYS = 64
_DEFAULT_CHUNK_BYTES = 1024 * 1024
_MIN_CHUNK_BYTES = 64 * 1024
_MAX_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_ATTESTATION_LIFETIME = timedelta(hours=24)
_POSIX_LOST_FOUND_NAME = "lost+found"
_KEY_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_KEY_ID_START_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


class AtRestError(IntegrityError):
    """The configured at-rest boundary cannot be established."""


class BackupKeyUnavailableError(AtRestError):
    """A backup key is missing, invalid, or does not match the archive."""


class BackupAuthenticationError(AtRestError):
    """Encrypted backup bytes failed format or authentication checks."""


@dataclass(frozen=True)
class BackupKeyMaterial:
    key_id: str
    key: bytes
    fingerprint: str
    status: Literal["active", "retired"]


class BackupKeyring:
    """External backup keys with explicit generations and retained old keys."""

    def __init__(
        self,
        *,
        generation: int,
        active_key_id: str,
        keys: tuple[BackupKeyMaterial, ...],
    ):
        if type(generation) is not int or generation < 1:
            raise ValueError("backup keyring generation is invalid")
        if not keys or len(keys) > _MAX_KEYS:
            raise ValueError("backup keyring key count is invalid")
        by_id = {item.key_id: item for item in keys}
        if len(by_id) != len(keys) or active_key_id not in by_id:
            raise ValueError("backup keyring key IDs are invalid")
        if sum(item.status == "active" for item in keys) != 1:
            raise ValueError("backup keyring requires exactly one active key")
        if by_id[active_key_id].status != "active":
            raise ValueError("backup keyring active key metadata is inconsistent")
        self.generation = generation
        self.active_key_id = active_key_id
        self._keys = by_id

    @property
    def active(self) -> BackupKeyMaterial:
        return self._keys[self.active_key_id]

    @classmethod
    def from_path(cls, path: Path | str) -> BackupKeyring:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            raise BackupKeyUnavailableError("backup keyring path cannot be a symlink")
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise BackupKeyUnavailableError("backup keyring is unavailable")
        try:
            if resolved.stat().st_nlink != 1:
                raise BackupKeyUnavailableError("backup keyring must not be hard-linked")
        except OSError as error:
            raise BackupKeyUnavailableError("backup keyring metadata is unavailable") from error
        with resolved.open("rb") as stream:
            raw = stream.read(_MAX_KEYRING_BYTES + 1)
        if not raw or len(raw) > _MAX_KEYRING_BYTES:
            raise BackupKeyUnavailableError("backup keyring size is invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BackupKeyUnavailableError("backup keyring JSON is invalid") from error
        if not isinstance(payload, dict) or set(payload) != {
            "format",
            "generation",
            "active_key_id",
            "keys",
        }:
            raise BackupKeyUnavailableError("backup keyring structure is invalid")
        if payload["format"] != BACKUP_KEYRING_FORMAT:
            raise BackupKeyUnavailableError("backup keyring format is unsupported")
        generation = payload["generation"]
        active_key_id = payload["active_key_id"]
        raw_keys = payload["keys"]
        if (
            type(generation) is not int
            or not isinstance(active_key_id, str)
            or not isinstance(raw_keys, list)
        ):
            raise BackupKeyUnavailableError("backup keyring fields are invalid")
        materials: list[BackupKeyMaterial] = []
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict) or set(raw_key) != {"key_id", "status", "key_b64"}:
                raise BackupKeyUnavailableError("backup keyring entry is invalid")
            key_id = raw_key["key_id"]
            status = raw_key["status"]
            encoded = raw_key["key_b64"]
            if (
                not isinstance(key_id, str)
                or status not in {"active", "retired"}
                or not isinstance(encoded, str)
            ):
                raise BackupKeyUnavailableError("backup keyring entry fields are invalid")
            cls._validate_key_id(key_id)
            try:
                key = cls._decode_key(encoded)
            except ValueError as error:
                raise BackupKeyUnavailableError("backup key material is invalid") from error
            materials.append(
                BackupKeyMaterial(
                    key_id=key_id,
                    key=key,
                    fingerprint=hashlib.sha256(key).hexdigest(),
                    status=status,
                )
            )
        try:
            return cls(
                generation=generation,
                active_key_id=active_key_id,
                keys=tuple(materials),
            )
        except ValueError as error:
            raise BackupKeyUnavailableError(str(error)) from error

    @classmethod
    def initialize(cls, path: Path | str) -> BackupKeyring:
        target = Path(path).expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"backup keyring already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        fingerprint = hashlib.sha256(key).hexdigest()
        material = BackupKeyMaterial(
            key_id=f"backup-1-{fingerprint[:12]}",
            key=key,
            fingerprint=fingerprint,
            status="active",
        )
        keyring = cls(generation=1, active_key_id=material.key_id, keys=(material,))
        keyring.write(target, create_only=True)
        return keyring

    @classmethod
    def rotate(cls, path: Path | str) -> BackupKeyring:
        target = Path(path).expanduser().resolve()
        current = cls.from_path(target)
        if len(current._keys) >= _MAX_KEYS:
            raise ValueError("backup keyring reached its retained-key limit")
        key = os.urandom(32)
        fingerprint = hashlib.sha256(key).hexdigest()
        generation = current.generation + 1
        active_key_id = f"backup-{generation}-{fingerprint[:12]}"
        materials = (
            *(
                BackupKeyMaterial(item.key_id, item.key, item.fingerprint, "retired")
                for item in current._keys.values()
            ),
            BackupKeyMaterial(active_key_id, key, fingerprint, "active"),
        )
        rotated = cls(
            generation=generation,
            active_key_id=active_key_id,
            keys=materials,
        )
        rotated.write(target, create_only=False)
        return rotated

    def resolve(self, key_id: object, fingerprint: object) -> BackupKeyMaterial:
        if not isinstance(key_id, str) or not isinstance(fingerprint, str):
            raise BackupKeyUnavailableError("backup key metadata is invalid")
        material = self._keys.get(key_id)
        if material is None or material.fingerprint != fingerprint:
            raise BackupKeyUnavailableError(f"backup key is unavailable: {key_id}")
        return material

    def metadata(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "active_key_id": self.active_key_id,
            "active_key_fingerprint": self.active.fingerprint,
            "retained_keys": len(self._keys),
        }

    def write(self, path: Path | str, *, create_only: bool) -> None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if create_only and target.exists():
            raise FileExistsError(f"backup keyring already exists: {target}")
        payload = {
            "format": BACKUP_KEYRING_FORMAT,
            "generation": self.generation,
            "active_key_id": self.active_key_id,
            "keys": [
                {
                    "key_id": item.key_id,
                    "status": item.status,
                    "key_b64": base64.urlsafe_b64encode(item.key).decode("ascii"),
                }
                for item in sorted(self._keys.values(), key=lambda value: value.key_id)
            ],
        }
        temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            try:
                raw = canonical_json(payload).encode("utf-8")
                _write_descriptor(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        try:
            with suppress(OSError):
                os.chmod(temporary, 0o600)
            if os.name == "nt":
                _windows_harden(temporary)
            temporary.replace(target)
            _fsync_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_key_id(key_id: str) -> None:
        if (
            not key_id
            or len(key_id) > 128
            or key_id[0] not in _KEY_ID_START_CHARACTERS
            or any(character not in _KEY_ID_CHARACTERS for character in key_id)
        ):
            raise BackupKeyUnavailableError("backup key ID is invalid")

    @staticmethod
    def _decode_key(value: str) -> bytes:
        try:
            key = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeError, ValueError) as error:
            raise ValueError("backup key is not canonical base64url") from error
        if len(key) != 32 or base64.urlsafe_b64encode(key).decode("ascii") != value:
            raise ValueError("backup key must contain 32 bytes")
        return key


@dataclass(frozen=True)
class VolumeEncryptionStatus:
    encrypted: bool
    backend: str
    detail: str
    volume_id: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "encrypted": self.encrypted,
            "backend": self.backend,
            "detail": self.detail,
            "volume_id": self.volume_id,
        }


@dataclass(frozen=True)
class AtRestConfig:
    data_root: Path
    mode: Literal["development", "required"] = "development"
    volume_backend: Literal["auto", "attestation"] = "auto"
    attestation_path: Path | None = None
    backup_keyring_path: Path | None = None

    @classmethod
    def from_env(cls, data_root: Path | str) -> AtRestConfig:
        raw_mode = os.getenv("NOYRA_AT_REST_MODE", "development").strip().lower()
        if raw_mode not in {"development", "required"}:
            raise ValueError("NOYRA_AT_REST_MODE must be development or required")
        raw_backend = os.getenv("NOYRA_VOLUME_ENCRYPTION_BACKEND", "auto").strip().lower()
        if raw_backend not in {"auto", "attestation"}:
            raise ValueError("NOYRA_VOLUME_ENCRYPTION_BACKEND must be auto or attestation")
        attestation = os.getenv("NOYRA_VOLUME_ATTESTATION_PATH", "").strip()
        keyring = os.getenv("NOYRA_BACKUP_KEYRING_PATH", "").strip()
        return cls(
            data_root=Path(data_root),
            mode=cast(Literal["development", "required"], raw_mode),
            volume_backend=cast(Literal["auto", "attestation"], raw_backend),
            attestation_path=Path(attestation) if attestation else None,
            backup_keyring_path=Path(keyring) if keyring else None,
        )


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class VolumeEncryptionProbe:
    """Native BitLocker/LUKS checks plus a root-owned container attestation."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        runner: CommandRunner | None = None,
        sysfs_root: Path | str = "/sys",
    ):
        self.platform_name = platform_name or os.name
        self.runner = runner or _run_command
        self.sysfs_root = Path(sysfs_root)

    def probe(
        self,
        data_root: Path | str,
        *,
        backend: Literal["auto", "attestation"],
        attestation_path: Path | None,
    ) -> VolumeEncryptionStatus:
        root = Path(data_root).expanduser().resolve()
        if backend == "attestation":
            if attestation_path is None:
                return VolumeEncryptionStatus(False, "attestation", "attestation path is missing")
            return self._attestation(root, attestation_path)
        if self.platform_name == "nt":
            return self._bitlocker(root)
        if self.platform_name == "posix":
            return self._luks(root)
        return VolumeEncryptionStatus(False, "unsupported", "platform probe is unavailable")

    def _bitlocker(self, root: Path) -> VolumeEncryptionStatus:
        encoded_root = base64.b64encode(str(root).encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_root}'))
$drive = (Get-Item -LiteralPath $root).PSDrive.Root
$volume = Get-BitLockerVolume -MountPoint $drive
[pscustomobject]@{{
  MountPoint = $volume.MountPoint
  VolumeStatus = $volume.VolumeStatus.ToString()
  ProtectionStatus = $volume.ProtectionStatus.ToString()
  EncryptionPercentage = [int]$volume.EncryptionPercentage
}} | ConvertTo-Json -Compress
"""
        try:
            result = self.runner(_powershell_command(script))
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or "BitLocker command failed")
            payload = json.loads(result.stdout)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return VolumeEncryptionStatus(
                False, "bitlocker", f"probe unavailable: {type(error).__name__}"
            )
        if not isinstance(payload, dict):
            return VolumeEncryptionStatus(False, "bitlocker", "BitLocker response is invalid")
        encrypted = (
            payload.get("VolumeStatus") == "FullyEncrypted"
            and payload.get("ProtectionStatus") == "On"
            and payload.get("EncryptionPercentage") == 100
        )
        return VolumeEncryptionStatus(
            encrypted,
            "bitlocker",
            "fully encrypted and protected" if encrypted else "volume protection is incomplete",
            str(payload.get("MountPoint")) if payload.get("MountPoint") else None,
        )

    def _luks(self, root: Path) -> VolumeEncryptionStatus:
        try:
            device = root.stat().st_dev
            major = _device_major(device)
            minor = _device_minor(device)
        except (AttributeError, OSError) as error:
            return VolumeEncryptionStatus(
                False, "luks", f"device lookup failed: {type(error).__name__}"
            )
        start = self.sysfs_root / "dev" / "block" / f"{major}:{minor}"
        visited: set[Path] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            try:
                resolved = current.resolve(strict=True)
            except OSError:
                continue
            if resolved in visited:
                continue
            visited.add(resolved)
            dm_uuid = resolved / "dm" / "uuid"
            try:
                value = dm_uuid.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value.startswith("CRYPT-LUKS"):
                return VolumeEncryptionStatus(True, "luks", "dm-crypt LUKS mapping verified", value)
            slaves = resolved / "slaves"
            with suppress(OSError):
                pending.extend(slaves.iterdir())
        return VolumeEncryptionStatus(False, "luks", "no dm-crypt LUKS ancestor was found")

    def _attestation(self, root: Path, path: Path) -> VolumeEncryptionStatus:
        candidate = path.expanduser()
        if candidate.is_symlink():
            return VolumeEncryptionStatus(False, "attestation", "attestation cannot be a symlink")
        resolved = candidate.resolve()
        permission_error = _attestation_permission_error(resolved, self.platform_name, self.runner)
        if permission_error is not None:
            return VolumeEncryptionStatus(False, "attestation", permission_error)
        try:
            raw = resolved.read_bytes()
            if not raw or len(raw) > _MAX_HEADER_BYTES:
                raise ValueError("size")
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            return VolumeEncryptionStatus(
                False,
                "attestation",
                f"attestation is unreadable or invalid: {type(error).__name__}",
            )
        expected_fields = {
            "format",
            "data_root",
            "encrypted",
            "provider",
            "volume_id",
            "expires_at",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            return VolumeEncryptionStatus(False, "attestation", "attestation fields are invalid")
        try:
            attested_root = Path(str(payload["data_root"])).expanduser().resolve()
            expires_at = _parse_utc(str(payload["expires_at"]))
        except (OSError, TypeError, ValueError):
            return VolumeEncryptionStatus(False, "attestation", "attestation values are invalid")
        provider = payload["provider"]
        volume_id = payload["volume_id"]
        if (
            payload["format"] != VOLUME_ATTESTATION_FORMAT
            or payload["encrypted"] is not True
            or attested_root != root
            or not isinstance(provider, str)
            or provider not in {"host-luks", "host-bitlocker", "cloud-kms"}
            or not isinstance(volume_id, str)
            or not volume_id.strip()
        ):
            return VolumeEncryptionStatus(
                False, "attestation", "attestation does not match data root"
            )
        now = datetime.now(UTC)
        if expires_at <= now:
            return VolumeEncryptionStatus(False, "attestation", "attestation is expired")
        if expires_at > now + _MAX_ATTESTATION_LIFETIME:
            return VolumeEncryptionStatus(
                False, "attestation", "attestation lifetime exceeds 24 hours"
            )
        return VolumeEncryptionStatus(
            True, "attestation", f"root-owned {provider} attestation", volume_id
        )


class AtRestGuard:
    """Prepare and continuously report the runtime's at-rest boundary."""

    def __init__(self, config: AtRestConfig, *, probe: VolumeEncryptionProbe | None = None):
        self.config = config
        configured_root = config.data_root.expanduser()
        if _contains_reparse_component(configured_root):
            raise AtRestError("at-rest data root cannot contain a symlink or reparse point")
        self.data_root = configured_root.resolve()
        self.probe = probe or VolumeEncryptionProbe()
        self._volume: VolumeEncryptionStatus | None = None
        self._prepared = False

    @property
    def required(self) -> bool:
        return self.config.mode == "required"

    def prepare(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        if not self.required:
            self._prepared = True
            return
        _harden_private_paths(self.data_root)
        self._require_volume()
        self._require_keyring()
        permission_error = _private_permission_error(self.data_root)
        if permission_error is not None:
            raise AtRestError(permission_error)
        if os.name != "nt":
            os.umask(0o077)
        self._prepared = True

    def post_initialize(self) -> None:
        if not self.required:
            return
        _harden_private_paths(self.data_root)
        permission_error = _private_permission_error(self.data_root)
        if permission_error is not None:
            raise AtRestError(permission_error)

    def require_ready(self) -> dict[str, Any]:
        if not self.required:
            return self.health()
        if not self._prepared:
            raise AtRestError("at-rest guard was not prepared")
        self._require_volume()
        keyring = self._require_keyring()
        permission_error = _private_permission_error(self.data_root)
        if permission_error is not None:
            raise AtRestError(permission_error)
        return self._health_payload(True, None, keyring)

    def health(self) -> dict[str, Any]:
        if not self.required:
            return {
                "mode": "development",
                "enforced": False,
                "ready": False,
                "threat_model": AT_REST_THREAT_MODEL,
                "permissions": {"ready": False, "detail": "not enforced"},
                "volume": {
                    "encrypted": False,
                    "backend": "not-enforced",
                    "detail": "development mode does not claim at-rest protection",
                    "volume_id": None,
                },
                "backup_key": {"configured": False},
            }
        try:
            self._require_volume()
            keyring = self._require_keyring()
            permission_error = _private_permission_error(self.data_root)
            if permission_error is not None:
                raise AtRestError(permission_error)
            return self._health_payload(True, None, keyring)
        except Exception as error:
            return self._health_payload(False, str(error), None)

    def _require_volume(self) -> VolumeEncryptionStatus:
        volume = self.probe.probe(
            self.data_root,
            backend=self.config.volume_backend,
            attestation_path=self.config.attestation_path,
        )
        self._volume = volume
        if not volume.encrypted:
            raise AtRestError(f"encrypted volume requirement failed: {volume.detail}")
        return volume

    def _require_keyring(self) -> BackupKeyring:
        path = self.config.backup_keyring_path
        if path is None:
            raise BackupKeyUnavailableError("NOYRA_BACKUP_KEYRING_PATH is required")
        resolved = validate_keyring_path(path)
        if resolved == self.data_root or self.data_root in resolved.parents:
            raise BackupKeyUnavailableError(
                "backup keyring must remain outside the backed-up data root"
            )
        return BackupKeyring.from_path(resolved)

    def _health_payload(
        self,
        ready: bool,
        reason: str | None,
        keyring: BackupKeyring | None,
    ) -> dict[str, Any]:
        volume = self._volume or VolumeEncryptionStatus(False, "unknown", "not probed")
        backup_key: dict[str, Any] = {"configured": keyring is not None}
        if keyring is not None:
            backup_key.update(keyring.metadata())
        payload: dict[str, Any] = {
            "mode": "required",
            "enforced": True,
            "ready": ready,
            "threat_model": AT_REST_THREAT_MODEL,
            "permissions": {
                "ready": ready,
                "detail": "private" if ready else "unverified",
            },
            "volume": volume.public(),
            "backup_key": backup_key,
        }
        if reason:
            payload["reason"] = reason
        return payload


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    byte_size: int
    content_hash: str
    key_id: str
    key_fingerprint: str
    keyring_generation: int
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "byte_size": self.byte_size,
            "content_hash": self.content_hash,
            "key_id": self.key_id,
            "key_fingerprint": self.key_fingerprint,
            "keyring_generation": self.keyring_generation,
            "created_at": self.created_at,
        }


class EncryptedBackupManager:
    """Create and restore bounded, authenticated, offline data-root backups."""

    def __init__(
        self,
        data_root: Path | str,
        keyring_path: Path | str,
        *,
        max_files: int = 100_000,
        max_total_bytes: int = 100_000_000_000,
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
    ):
        if max_files < 1 or max_files > 1_000_000:
            raise ValueError("backup file limit is invalid")
        if max_total_bytes < 1_000_000 or max_total_bytes > 10_000_000_000_000:
            raise ValueError("backup byte limit is invalid")
        if chunk_bytes < _MIN_CHUNK_BYTES or chunk_bytes > _MAX_CHUNK_BYTES:
            raise ValueError("backup chunk size is invalid")
        configured_root = Path(data_root).expanduser()
        configured_keyring = Path(keyring_path).expanduser()
        if _contains_reparse_component(configured_root):
            raise AtRestError("backup data root cannot contain a symlink or reparse point")
        if _contains_reparse_component(configured_keyring):
            raise BackupKeyUnavailableError(
                "backup keyring path cannot contain a symlink or reparse point"
            )
        self.data_root = configured_root.resolve()
        self.keyring_path = configured_keyring.resolve()
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.chunk_bytes = chunk_bytes

    def create(self, output_path: Path | str) -> BackupArtifact:
        configured_output = Path(output_path).expanduser()
        if _contains_reparse_component(configured_output):
            raise AtRestError("backup destination cannot contain a symlink or reparse point")
        output = configured_output.resolve()
        if output.exists():
            raise FileExistsError(f"backup already exists: {output}")
        if output == self.data_root or self.data_root in output.parents:
            raise ValueError("backup destination must be outside the data root")
        if self.keyring_path == self.data_root or self.data_root in self.keyring_path.parents:
            raise ValueError("backup keyring must be outside the data root")
        keyring = self._keyring()
        database_path = self.data_root / "noyra.sqlite3"
        if not database_path.is_file():
            raise FileNotFoundError(f"runtime database is unavailable: {database_path}")
        lock = ProcessLock(database_path.with_name(database_path.name + ".lock"))
        try:
            lock.acquire()
        except RuntimeOwnershipError as error:
            raise RuntimeOwnershipError("stop Noyra before creating an at-rest backup") from error
        work_root = Path(tempfile.mkdtemp(prefix=".noyra-backup-", dir=self.data_root))
        payload_root = work_root / "payload"
        tar_path = work_root / "payload.tar"
        created_at = utc_now()
        try:
            payload_root.mkdir(mode=0o700)
            self._snapshot_database(database_path, payload_root / "noyra.sqlite3")
            self._copy_persistent_tree(payload_root, work_root)
            manifest = self._manifest(payload_root, created_at)
            manifest_bytes = canonical_json(manifest).encode("utf-8")
            if len(manifest_bytes) > self._manifest_byte_limit():
                raise AtRestError("backup manifest exceeds its configured bound")
            _write_private_file(
                payload_root / "manifest.json",
                manifest_bytes,
            )
            self._write_tar(payload_root, tar_path)
            header = self._encrypt(tar_path, output, keyring, created_at)
            byte_size = output.stat().st_size
            return BackupArtifact(
                path=output,
                byte_size=byte_size,
                content_hash=_file_hash(output),
                key_id=str(header["key_id"]),
                key_fingerprint=str(header["key_fingerprint"]),
                keyring_generation=int(header["keyring_generation"]),
                created_at=created_at,
            )
        finally:
            shutil.rmtree(work_root, ignore_errors=True)
            lock.release()

    def restore(self, backup_path: Path | str, target_root: Path | str) -> Path:
        configured_source = Path(backup_path).expanduser()
        configured_target = Path(target_root).expanduser()
        if _contains_reparse_component(configured_source):
            raise BackupAuthenticationError(
                "backup source cannot contain a symlink or reparse point"
            )
        if _contains_reparse_component(configured_target):
            raise AtRestError("restore target cannot contain a symlink or reparse point")
        source = configured_source.resolve()
        target = configured_target.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"backup is unavailable: {source}")
        target_preexisting = target.exists()
        if target_preexisting and (not target.is_dir() or any(target.iterdir())):
            raise FileExistsError("restore target must be absent or an empty directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target_preexisting and target.stat().st_dev != target.parent.stat().st_dev:
            raise AtRestError(
                "restore target cannot be a mount point; use a child directory "
                "on the encrypted volume"
            )
        work_root = Path(tempfile.mkdtemp(prefix=".noyra-restore-", dir=target.parent))
        tar_path = work_root / "payload.tar"
        restored = work_root / "restored"
        restored.mkdir(mode=0o700)
        try:
            self._decrypt(source, tar_path, self._keyring())
            self._extract_tar(tar_path, restored)
            tar_path.unlink(missing_ok=True)
            self._verify_manifest(restored)
            self._verify_database(restored / "noyra.sqlite3")
            _harden_tree(restored)
            if target.exists():
                target.rmdir()
            restored.replace(target)
            _fsync_directory(target.parent)
            return target
        except Exception:
            if target_preexisting and not target.exists():
                target.mkdir(mode=0o700)
            raise
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    def inspect(self, backup_path: Path | str) -> dict[str, Any]:
        with Path(backup_path).expanduser().resolve().open("rb") as stream:
            return self._read_header(stream)

    def _keyring(self) -> BackupKeyring:
        return BackupKeyring.from_path(validate_keyring_path(self.keyring_path))

    @staticmethod
    def _snapshot_database(source_path: Path, target_path: Path) -> None:
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(target_path, timeout=30)
        try:
            source.backup(target, pages=256)
            target.commit()
            result = target.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise AtRestError("database snapshot failed SQLite quick_check")
        finally:
            target.close()
            source.close()
            with suppress(OSError):
                os.chmod(target_path, 0o600)

    def _copy_persistent_tree(self, payload_root: Path, work_root: Path) -> None:
        database_size = (payload_root / "noyra.sqlite3").stat().st_size
        budget = {"files": 1, "bytes": database_size}
        if database_size > self.max_total_bytes:
            raise AtRestError("backup exceeds its configured file or byte limit")
        skipped_names = {
            "noyra.sqlite3",
            "noyra.sqlite3-wal",
            "noyra.sqlite3-shm",
            "noyra.sqlite3.lock",
            "cache",
        }
        for source in _private_root_entries(self.data_root):
            if source == work_root or source.name in skipped_names:
                continue
            if source.name.startswith("noyra.sqlite3.pre-migration-v") and source.name.endswith(
                ".bak"
            ):
                # These verified images exist solely for an in-process schema
                # rollback.  Successful initialization removes them; a stale
                # copy must not silently duplicate an older private database
                # generation in the durable offline backup.
                continue
            if source.name.startswith(".noyra-backup-") or source.name.startswith(
                ".noyra-restore-"
            ):
                continue
            if source.name.startswith(".noyra.sqlite3."):
                continue
            destination = payload_root / source.name
            self._copy_entry(source, destination, budget)

    def _copy_entry(self, source: Path, destination: Path, budget: dict[str, int]) -> None:
        if _is_reparse_entry(source):
            raise AtRestError(f"backup source contains a symlink or reparse point: {source.name}")
        if source.is_dir():
            destination.mkdir(mode=0o700)
            for child in sorted(source.iterdir(), key=lambda item: item.name):
                self._copy_entry(child, destination / child.name, budget)
            return
        if not source.is_file():
            raise AtRestError(f"backup source is not a regular file: {source.name}")
        source_size = source.stat().st_size
        budget["files"] += 1
        budget["bytes"] += source_size
        if budget["files"] > self.max_files or budget["bytes"] > self.max_total_bytes:
            raise AtRestError("backup exceeds its configured file or byte limit")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(),
            0o600,
        )
        try:
            with (
                source.open("rb") as input_stream,
                os.fdopen(descriptor, "wb", closefd=False) as out,
            ):
                remaining = source_size
                while remaining:
                    chunk = input_stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise AtRestError("backup source changed while it was copied")
                    out.write(chunk)
                    remaining -= len(chunk)
                if input_stream.read(1):
                    raise AtRestError("backup source changed while it was copied")
                out.flush()
                os.fsync(out.fileno())
        finally:
            os.close(descriptor)

    def _manifest(self, payload_root: Path, created_at: str) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for path in _regular_files(payload_root):
            relative = path.relative_to(payload_root).as_posix()
            size = path.stat().st_size
            total_bytes += size
            if len(files) + 1 > self.max_files or total_bytes > self.max_total_bytes:
                raise AtRestError("backup exceeds its configured file or byte limit")
            files.append({"path": relative, "byte_size": size, "sha256": _file_hash(path)})
        return {
            "format": BACKUP_MANIFEST_FORMAT,
            "created_at": created_at,
            "files": files,
            "total_bytes": total_bytes,
        }

    @staticmethod
    def _write_tar(payload_root: Path, tar_path: Path) -> None:
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for path in _walk_private_tree(payload_root):
                relative = path.relative_to(payload_root).as_posix()
                info = tarfile.TarInfo(relative)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o700
                    archive.addfile(info)
                elif path.is_file():
                    info.size = path.stat().st_size
                    info.mode = 0o600
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    raise AtRestError("backup staging contains a non-regular entry")
        with suppress(OSError):
            os.chmod(tar_path, 0o600)

    def _encrypt(
        self,
        tar_path: Path,
        output: Path,
        keyring: BackupKeyring,
        created_at: str,
    ) -> dict[str, Any]:
        material = keyring.active
        nonce_prefix = os.urandom(8)
        header = {
            "format": BACKUP_FORMAT,
            "created_at": created_at,
            "key_id": material.key_id,
            "key_fingerprint": material.fingerprint,
            "keyring_generation": keyring.generation,
            "chunk_bytes": self.chunk_bytes,
            "nonce_prefix_b64": base64.urlsafe_b64encode(nonce_prefix).decode("ascii"),
        }
        header_bytes = canonical_json(header).encode("utf-8")
        header_hash = hashlib.sha256(header_bytes).digest()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.urandom(8).hex()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            try:
                with (
                    os.fdopen(descriptor, "wb", closefd=False) as encrypted,
                    tar_path.open("rb") as raw,
                ):
                    encrypted.write(_BACKUP_MAGIC)
                    encrypted.write(_HEADER_LENGTH.pack(len(header_bytes)))
                    encrypted.write(header_bytes)
                    aes = AESGCM(material.key)
                    index = 0
                    while chunk := raw.read(self.chunk_bytes):
                        if index >= 2**32:
                            raise AtRestError("backup exceeds AES-GCM nonce capacity")
                        record = _RECORD_HEADER.pack(_DATA_RECORD, index, len(chunk))
                        nonce = nonce_prefix + index.to_bytes(4, "big")
                        encrypted.write(record)
                        encrypted.write(aes.encrypt(nonce, chunk, header_hash + record))
                        index += 1
                    record = _RECORD_HEADER.pack(_END_RECORD, index, 0)
                    nonce = nonce_prefix + index.to_bytes(4, "big")
                    encrypted.write(record)
                    encrypted.write(aes.encrypt(nonce, b"", header_hash + record))
                    encrypted.flush()
                    os.fsync(encrypted.fileno())
            finally:
                os.close(descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        try:
            with suppress(OSError):
                os.chmod(temporary, 0o600)
            if os.name == "nt":
                _windows_harden(temporary)
            temporary.replace(output)
            _fsync_directory(output.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return header

    def _decrypt(self, source: Path, target: Path, keyring: BackupKeyring) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(target, flags, 0o600)
        raw = os.fdopen(descriptor, "wb")
        try:
            with source.open("rb") as encrypted, raw:
                header = self._read_header(encrypted)
                material = keyring.resolve(header["key_id"], header["key_fingerprint"])
                header_bytes = canonical_json(header).encode("utf-8")
                header_hash = hashlib.sha256(header_bytes).digest()
                try:
                    nonce_prefix = base64.b64decode(
                        str(header["nonce_prefix_b64"]).encode("ascii"),
                        altchars=b"-_",
                        validate=True,
                    )
                except (UnicodeError, ValueError) as error:
                    raise BackupAuthenticationError("backup nonce is invalid") from error
                if len(nonce_prefix) != 8:
                    raise BackupAuthenticationError("backup nonce is invalid")
                aes = AESGCM(material.key)
                expected_index = 0
                total_bytes = 0
                while True:
                    record = _read_exact(encrypted, _RECORD_HEADER.size)
                    record_type, index, plain_size = _RECORD_HEADER.unpack(record)
                    if index != expected_index or index >= 2**32:
                        raise BackupAuthenticationError("backup chunk sequence is invalid")
                    if plain_size > int(header["chunk_bytes"]):
                        raise BackupAuthenticationError("backup chunk size is invalid")
                    ciphertext = _read_exact(encrypted, plain_size + 16)
                    nonce = nonce_prefix + index.to_bytes(4, "big")
                    try:
                        plaintext = aes.decrypt(nonce, ciphertext, header_hash + record)
                    except InvalidTag as error:
                        raise BackupAuthenticationError("backup authentication failed") from error
                    if record_type == _END_RECORD:
                        if plain_size != 0 or plaintext or encrypted.read(1):
                            raise BackupAuthenticationError("backup terminator is invalid")
                        break
                    if record_type != _DATA_RECORD or len(plaintext) != plain_size:
                        raise BackupAuthenticationError("backup record is invalid")
                    total_bytes += len(plaintext)
                    if total_bytes > self._tar_byte_limit():
                        raise AtRestError("decrypted backup exceeds its configured bound")
                    raw.write(plaintext)
                    expected_index += 1
                raw.flush()
                os.fsync(raw.fileno())
        except Exception:
            raw.close()
            target.unlink(missing_ok=True)
            raise

    def _read_header(self, stream: Any) -> dict[str, Any]:
        if _read_exact(stream, len(_BACKUP_MAGIC)) != _BACKUP_MAGIC:
            raise BackupAuthenticationError("backup magic is invalid")
        header_size = _HEADER_LENGTH.unpack(_read_exact(stream, _HEADER_LENGTH.size))[0]
        if header_size < 1 or header_size > _MAX_HEADER_BYTES:
            raise BackupAuthenticationError("backup header size is invalid")
        try:
            payload = json.loads(_read_exact(stream, header_size).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BackupAuthenticationError("backup header JSON is invalid") from error
        expected = {
            "format",
            "created_at",
            "key_id",
            "key_fingerprint",
            "keyring_generation",
            "chunk_bytes",
            "nonce_prefix_b64",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise BackupAuthenticationError("backup header fields are invalid")
        if (
            payload["format"] != BACKUP_FORMAT
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["key_id"], str)
            or not isinstance(payload["key_fingerprint"], str)
            or len(payload["key_fingerprint"]) != 64
            or type(payload["keyring_generation"]) is not int
            or payload["keyring_generation"] < 1
            or type(payload["chunk_bytes"]) is not int
            or not _MIN_CHUNK_BYTES <= payload["chunk_bytes"] <= _MAX_CHUNK_BYTES
            or not isinstance(payload["nonce_prefix_b64"], str)
        ):
            raise BackupAuthenticationError("backup header values are invalid")
        try:
            _parse_utc(payload["created_at"])
        except ValueError as error:
            raise BackupAuthenticationError("backup creation timestamp is invalid") from error
        return payload

    def _extract_tar(self, tar_path: Path, target: Path) -> None:
        file_count = 0
        total_bytes = 0
        with tarfile.open(tar_path, "r|") as archive:
            for member in archive:
                relative = _safe_archive_path(member.name)
                destination = target.joinpath(*relative.parts)
                if member.isdir():
                    if _contains_reparse_component(destination):
                        raise BackupAuthenticationError("backup directory is a reparse point")
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                if not member.isfile():
                    raise BackupAuthenticationError("backup contains a link or special file")
                if type(member.size) is not int or member.size < 0:
                    raise BackupAuthenticationError("backup member size is invalid")
                if _contains_reparse_component(destination.parent):
                    raise BackupAuthenticationError("backup destination contains a reparse point")
                file_count += 1
                total_bytes += member.size
                if (
                    file_count > self.max_files + 1
                    or total_bytes > self.max_total_bytes + self._manifest_byte_limit()
                ):
                    raise AtRestError("restored backup exceeds its configured bounds")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupAuthenticationError("backup member payload is unavailable")
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(),
                    0o600,
                )
                try:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise BackupAuthenticationError("backup member is truncated")
                        _write_descriptor(descriptor, chunk)
                        remaining -= len(chunk)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def _verify_manifest(self, restored: Path) -> None:
        manifest_path = restored / "manifest.json"
        try:
            raw = manifest_path.read_bytes()
            if len(raw) > self._manifest_byte_limit():
                raise ValueError("manifest too large")
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise BackupAuthenticationError("backup manifest is invalid") from error
        if not isinstance(manifest, dict) or set(manifest) != {
            "format",
            "created_at",
            "files",
            "total_bytes",
        }:
            raise BackupAuthenticationError("backup manifest fields are invalid")
        if manifest["format"] != BACKUP_MANIFEST_FORMAT or not isinstance(manifest["files"], list):
            raise BackupAuthenticationError("backup manifest format is invalid")
        expected: dict[str, tuple[int, str]] = {}
        total = 0
        for entry in manifest["files"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "byte_size", "sha256"}:
                raise BackupAuthenticationError("backup manifest entry is invalid")
            relative = _safe_archive_path(entry["path"])
            name = relative.as_posix()
            size = entry["byte_size"]
            digest = entry["sha256"]
            if (
                name == "manifest.json"
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or name in expected
            ):
                raise BackupAuthenticationError("backup manifest entry values are invalid")
            expected[name] = (size, digest)
            total += size
        actual_files = {
            path.relative_to(restored).as_posix(): path
            for path in _regular_files(restored)
            if path != manifest_path
        }
        if set(actual_files) != set(expected) or manifest["total_bytes"] != total:
            raise BackupAuthenticationError("backup manifest file set is inconsistent")
        for name, path in actual_files.items():
            size, digest = expected[name]
            if path.stat().st_size != size or _file_hash(path) != digest:
                raise BackupAuthenticationError(f"backup manifest mismatch: {name}")
        manifest_path.unlink()

    def _manifest_byte_limit(self) -> int:
        return min(256 * 1024 * 1024, max(2 * 1024 * 1024, self.max_files * 2_048))

    def _tar_byte_limit(self) -> int:
        tar_headers = (self.max_files + 2) * 1_024
        return self.max_total_bytes + self._manifest_byte_limit() + tar_headers + 10 * 1024 * 1024

    @staticmethod
    def _verify_database(path: Path) -> None:
        if not path.is_file():
            raise BackupAuthenticationError("restored database is missing")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            marker = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error as error:
            raise BackupAuthenticationError("restored database is unreadable") from error
        finally:
            connection.close()
        try:
            version = int(marker[0]) if marker is not None else 0
        except (TypeError, ValueError):
            version = 0
        if result is None or result[0] != "ok" or version < 1:
            raise BackupAuthenticationError("restored database failed integrity validation")


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def _powershell_command(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def _device_major(device: int) -> int:
    function = getattr(os, "major", None)
    if function is None:
        raise AttributeError("device major lookup is unavailable")
    return int(function(device))


def _device_minor(device: int) -> int:
    function = getattr(os, "minor", None)
    if function is None:
        raise AttributeError("device minor lookup is unavailable")
    return int(function(device))


def _effective_uid() -> int:
    function = getattr(os, "geteuid", None)
    if function is None:
        raise AttributeError("effective UID is unavailable")
    return int(function())


def _effective_gid() -> int:
    function = getattr(os, "getegid", None)
    if function is None:
        raise AttributeError("effective GID is unavailable")
    return int(function())


def _effective_groups() -> tuple[int, ...]:
    function = getattr(os, "getgroups", None)
    if function is None:
        raise AttributeError("supplementary groups are unavailable")
    return tuple(int(value) for value in function())


def _attestation_permission_error(
    path: Path,
    platform_name: str,
    runner: CommandRunner,
) -> str | None:
    if not path.is_file():
        return "attestation file is unavailable"
    if platform_name == "posix":
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            return "attestation must be root-owned and not group/other writable"
        return None
    if platform_name == "nt":
        result = _windows_permission_audit(
            path,
            runner=runner,
            require_current_owner=True,
            require_privileged_owner=True,
        )
        return None if result[0] else result[1]
    return "attestation permission probe is unsupported"


def _keyring_permission_error(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return "backup keyring is unavailable or is a symlink"
    try:
        link_count = path.stat().st_nlink
    except OSError as error:
        return f"backup keyring metadata is unavailable: {type(error).__name__}"
    if link_count != 1:
        return "backup keyring must not be hard-linked"
    if os.name == "nt":
        ready, detail = _windows_permission_audit(path)
        return None if ready else f"backup keyring ACL is unsafe: {detail}"
    stat = path.stat()
    effective_uid = _effective_uid()
    effective_groups = {_effective_gid(), *_effective_groups()}
    mode = stat.st_mode & 0o777
    if stat.st_uid == effective_uid:
        if mode & 0o077:
            return "backup keyring owned by the service account must use mode 0600"
        return None
    if stat.st_uid == 0 and stat.st_gid in effective_groups:
        if mode & 0o027 or not mode & 0o040:
            return "root-owned backup keyring must use a read-only service group mode such as 0640"
        return None
    return "backup keyring owner is not the service account or root"


def _private_permission_error(data_root: Path) -> str | None:
    if os.name == "nt":
        ready, detail = _windows_permission_audit(data_root)
        return None if ready else f"Windows private-storage ACL check failed: {detail}"
    effective_uid = _effective_uid()
    metadata: list[tuple[str, bool, int, int]] = []
    try:
        root_device = data_root.stat().st_dev
    except OSError as error:
        return f"private storage metadata is unavailable: {type(error).__name__}"
    for path in _private_paths(data_root):
        if _is_reparse_entry(path):
            return f"private storage contains a symlink: {path.name}"
        try:
            stat = path.stat()
        except OSError as error:
            return f"private storage metadata is unavailable: {type(error).__name__}"
        # A nested mount/junction is a separate trust boundary.  It must be
        # attested independently; accepting it would let a private root span
        # an unencrypted or operator-controlled device.
        if stat.st_dev != root_device:
            return f"private storage crosses a device boundary: {path.name}"
        metadata.append((path.name, path.is_dir(), stat.st_uid, stat.st_mode & 0o777))
    return _posix_metadata_error(metadata, effective_uid)


def _posix_metadata_error(
    metadata: list[tuple[str, bool, int, int]],
    effective_uid: int,
) -> str | None:
    for name, is_directory, owner_uid, mode in metadata:
        if owner_uid != effective_uid:
            return f"private storage is not owned by the service account: {name}"
        if is_directory and mode & 0o077:
            return f"private directory is accessible by group/other: {name}"
        if not is_directory and mode & 0o077:
            return f"private file is accessible by group/other: {name}"
    return None


def _contains_reparse_component(path: Path) -> bool:
    """Reject symlink/reparse ancestors before a private root is accepted."""
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if current.parent == current:
                return False
            current = current.parent
            continue
        except OSError:
            return True
        if stat.S_ISLNK(metadata.st_mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _is_reparse_entry(path: Path) -> bool:
    """Return true for links/reparse points without following the entry."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable path is treated as unsafe by all private-tree callers.
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _walk_private_tree(root: Path) -> Iterator[Path]:
    """Walk a private tree while refusing links/reparse points before descent."""
    if _is_reparse_entry(root):
        raise AtRestError("private tree contains a symlink or reparse point")

    def visit(current: Path) -> Iterator[Path]:
        try:
            entries = sorted(Path(entry.path) for entry in os.scandir(current))
        except OSError as error:
            raise AtRestError("private tree cannot be enumerated") from error
        for path in entries:
            if _is_reparse_entry(path):
                raise AtRestError("private tree contains a symlink or reparse point")
            try:
                is_directory = path.is_dir()
                is_file = path.is_file()
            except OSError as error:
                raise AtRestError("private tree metadata is unavailable") from error
            if not is_directory and not is_file:
                raise AtRestError("private tree contains a non-regular entry")
            yield path
            if is_directory:
                yield from visit(path)

    yield from visit(root)


def _harden_private_paths(data_root: Path) -> None:
    if os.name == "nt":
        _windows_harden(data_root, entire_tree=True)
        return
    for path in _private_paths(data_root):
        if path.is_symlink():
            raise AtRestError(f"private storage contains a symlink: {path.name}")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _harden_tree(root: Path) -> None:
    if os.name == "nt":
        _windows_harden(root, entire_tree=True)
        return
    paths = list(_walk_private_tree(root))
    for path in sorted(paths, key=lambda item: len(item.parts)):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(root, 0o700)


def _private_paths(data_root: Path) -> Iterator[Path]:
    yield data_root
    # Inspect every persistent entry, not only the historical ``secrets`` and
    # SQLite paths.  Workspace, subject archives, exports and training staging
    # can all contain private material and must obey the same contract.
    for path in _private_root_entries(data_root):
        try:
            if path.is_dir():
                yield path
                yield from _walk_private_tree(path)
            else:
                yield path
        except OSError as error:
            raise AtRestError("private storage metadata is unavailable") from error


def _private_root_entries(data_root: Path) -> Iterator[Path]:
    """Yield root children that belong to Noyra's private data boundary.

    A freshly formatted POSIX ext4 volume contains a root-owned, mode-0700
    ``lost+found`` directory. It is filesystem recovery metadata, not Noyra
    state, and the service account cannot chmod it. Only that exact root-level
    POSIX directory is excluded; every other unexpected or privileged entry
    remains part of the boundary and is checked fail-closed.
    """
    try:
        entries = sorted(data_root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise AtRestError("private storage cannot be enumerated") from error
    for path in entries:
        if os.name == "posix" and path.parent == data_root and path.name == _POSIX_LOST_FOUND_NAME:
            reserved_error = _posix_lost_found_error(data_root, path)
            if reserved_error is not None:
                raise AtRestError(reserved_error)
            continue
        if _is_reparse_entry(path):
            raise AtRestError("private storage contains a symlink or reparse point")
        try:
            if path.is_dir() or path.is_file():
                yield path
            else:
                raise AtRestError("private storage contains a non-regular entry")
        except OSError as error:
            raise AtRestError("private storage metadata is unavailable") from error


def _is_posix_lost_found(data_root: Path, path: Path) -> bool:
    """Recognize only the standard root-level POSIX ext4 recovery directory."""
    if os.name != "posix" or path.name != _POSIX_LOST_FOUND_NAME or path.parent != data_root:
        return False
    return _posix_lost_found_error(data_root, path) is None


def _posix_lost_found_error(data_root: Path, path: Path) -> str | None:
    """Return an error when a root-level ``lost+found`` entry is not standard."""
    if os.name != "posix" or path.name != _POSIX_LOST_FOUND_NAME or path.parent != data_root:
        return None
    try:
        root_metadata = data_root.lstat()
        metadata = path.lstat()
    except FileNotFoundError:
        return "private storage reserved directory lost+found is unavailable"
    except OSError as error:
        return f"private storage metadata is unavailable: {type(error).__name__}"
    if _is_reparse_entry(path):
        return "private storage reserved directory lost+found is a symlink or reparse point"
    if not stat.S_ISDIR(root_metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return "private storage reserved entry lost+found must be a directory"
    if metadata.st_dev != root_metadata.st_dev:
        return "private storage reserved directory lost+found crosses a device boundary"
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        return "private storage reserved directory lost+found must be root-owned"
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        return "private storage reserved directory lost+found must use mode 0700"
    return None


def validate_private_root(
    path: Path | str,
    *,
    create: bool = False,
    expected_device: int | None = None,
    label: str = "private root",
) -> Path:
    """Resolve a file-backed private root without crossing links or devices.

    This is intentionally shared by secret stores and at-rest checks.  The
    check is repeated after creation so a concurrently-created junction cannot
    be accepted between ``mkdir`` and the first write.
    """
    configured = Path(path).expanduser()
    if _contains_reparse_component(configured):
        raise AtRestError(f"{label} cannot contain a symlink or reparse point")
    boundary_device = expected_device
    if boundary_device is None:
        ancestor = configured.parent
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        try:
            if _contains_reparse_component(ancestor):
                raise AtRestError(f"{label} parent cannot contain a symlink or reparse point")
            boundary_device = ancestor.resolve(strict=True).stat().st_dev
        except (FileNotFoundError, OSError) as error:
            raise AtRestError(f"{label} parent is unavailable") from error
    if create:
        configured.mkdir(parents=True, exist_ok=True)
    if _contains_reparse_component(configured):
        raise AtRestError(f"{label} cannot contain a symlink or reparse point")
    try:
        resolved = configured.resolve(strict=True)
        metadata = resolved.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AtRestError(f"{label} is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_entry(resolved):
        raise AtRestError(f"{label} must be a regular directory")
    device = metadata.st_dev
    if boundary_device is not None and device != boundary_device:
        raise AtRestError(f"{label} crosses a device boundary")
    if create:
        _harden_tree(resolved)
    return resolved


def private_root_device(path: Path | str) -> int:
    """Return the device identity of a validated private root."""
    return validate_private_root(path, label="private root").stat().st_dev


def validate_private_file(
    root: Path | str,
    reference: str,
    *,
    expected_device: int | None = None,
    allow_missing: bool = True,
) -> Path:
    """Validate a single private file reference without following links."""
    if (
        not isinstance(reference, str)
        or not reference
        or Path(reference).name != reference
        or reference in {".", ".."}
        or "/" in reference
        or "\\" in reference
    ):
        raise AtRestError("private file reference is invalid")
    base = validate_private_root(root, label="private root")
    candidate = base / reference
    if _contains_reparse_component(candidate) or _is_reparse_entry(candidate):
        raise AtRestError("private file cannot be a symlink or reparse point")
    if not candidate.exists():
        if allow_missing:
            return candidate
        raise AtRestError("private file is unavailable")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AtRestError("private file metadata is unavailable") from error
    if resolved.parent != base or not stat.S_ISREG(metadata.st_mode):
        raise AtRestError("private file reference escapes its root")
    boundary_device = base.stat().st_dev if expected_device is None else expected_device
    if metadata.st_dev != boundary_device:
        raise AtRestError("private file crosses a device boundary")
    return resolved


def validate_keyring_path(path: Path | str) -> Path:
    """Validate a keyring path and its ACL before parsing key material."""
    configured = Path(path).expanduser()
    if _contains_reparse_component(configured):
        raise BackupKeyUnavailableError(
            "backup keyring path cannot contain a symlink or reparse point"
        )
    try:
        resolved = configured.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise BackupKeyUnavailableError("backup keyring is unavailable") from error
    permission_error = _keyring_permission_error(resolved)
    if permission_error is not None:
        raise BackupKeyUnavailableError(permission_error)
    return resolved


def _windows_harden(root: Path, *, entire_tree: bool = False) -> None:
    encoded_root = base64.b64encode(str(root).encode("utf-8")).decode("ascii")
    scope = "$true" if entire_tree else "$false"
    script = f"""
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_root}'))
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
function Set-PrivateAcl([string]$path, [bool]$directory) {{
  if ($directory) {{
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
  }} else {{
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
  }}
  $acl.SetOwner($identity.User)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sid in @($identity.User, $system, $admins)) {{
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
      $sid,
      [System.Security.AccessControl.FileSystemRights]::FullControl,
      $inheritance,
      [System.Security.AccessControl.PropagationFlags]::None,
      [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
  }}
  (Get-Item -Force -LiteralPath $path).SetAccessControl($acl)
}}
$targets = New-Object System.Collections.Generic.List[object]
$targets.Add((Get-Item -Force -LiteralPath $root))
if ({scope}) {{
  Get-ChildItem -Force -Recurse -LiteralPath $root | ForEach-Object {{ $targets.Add($_) }}
}} else {{
  $secrets = Join-Path $root 'secrets'
  if (Test-Path -LiteralPath $secrets) {{
    Get-ChildItem -Force -Recurse -LiteralPath $secrets | ForEach-Object {{ $targets.Add($_) }}
    $targets.Add((Get-Item -Force -LiteralPath $secrets))
  }}
  Get-ChildItem -Force -LiteralPath $root -Filter 'noyra.sqlite3*' |
    ForEach-Object {{ $targets.Add($_) }}
}}
foreach ($item in $targets) {{
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
    throw "private storage contains a reparse point: $($item.FullName)"
  }}
  Set-PrivateAcl $item.FullName $item.PSIsContainer
}}
"""
    result = _run_command(_powershell_command(script))
    if result.returncode != 0:
        raise AtRestError(result.stderr.strip() or "Windows ACL hardening failed")


def _windows_permission_audit(
    root: Path,
    *,
    runner: CommandRunner | None = None,
    require_current_owner: bool = True,
    require_privileged_owner: bool = False,
) -> tuple[bool, str]:
    run = runner or _run_command
    encoded_root = base64.b64encode(str(root).encode("utf-8")).decode("ascii")
    owner_check = "$true" if require_current_owner else "$false"
    privileged_owner_check = "$true" if require_privileged_owner else "$false"
    script = f"""
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_root}'))
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18', 'S-1-5-32-544')
$privileged = @('S-1-5-18', 'S-1-5-32-544')
$targets = New-Object System.Collections.Generic.List[object]
$targets.Add((Get-Item -Force -LiteralPath $root))
if ((Get-Item -Force -LiteralPath $root).PSIsContainer) {{
  Get-ChildItem -Force -Recurse -LiteralPath $root |
    ForEach-Object {{ $targets.Add($_) }}
}}
foreach ($item in $targets) {{
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
    [pscustomobject]@{{Ready=$false; Detail='reparse point'}} | ConvertTo-Json -Compress
    exit 0
  }}
  $acl = $item.GetAccessControl()
  try {{ $owner = (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate(
    [System.Security.Principal.SecurityIdentifier]).Value }} catch {{ $owner = $acl.Owner }}
  if ({owner_check} -and $allowed -notcontains $owner) {{
    [pscustomobject]@{{Ready=$false; Detail='unexpected owner'}} | ConvertTo-Json -Compress
    exit 0
  }}
  if ({privileged_owner_check} -and $privileged -notcontains $owner) {{
    [pscustomobject]@{{Ready=$false; Detail='owner is not system or administrators'}} |
      ConvertTo-Json -Compress
    exit 0
  }}
  foreach ($entry in $acl.Access) {{
    if ($entry.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {{
      continue
    }}
    try {{
      $sid = $entry.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]).Value
    }} catch {{
      $sid = $entry.IdentityReference.Value
    }}
    if ($allowed -notcontains $sid) {{
      [pscustomobject]@{{Ready=$false; Detail='access granted to another principal'}} |
        ConvertTo-Json -Compress
      exit 0
    }}
    if ({privileged_owner_check} -and $sid -eq $current -and $privileged -notcontains $current) {{
      $writeRights = [System.Security.AccessControl.FileSystemRights]::Write -bor
        [System.Security.AccessControl.FileSystemRights]::Modify -bor
        [System.Security.AccessControl.FileSystemRights]::FullControl -bor
        [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [System.Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [System.Security.AccessControl.FileSystemRights]::Delete
      if (($entry.FileSystemRights -band $writeRights) -ne 0) {{
        [pscustomobject]@{{Ready=$false; Detail='service account can modify attestation'}} |
          ConvertTo-Json -Compress
        exit 0
      }}
    }}
  }}
}}
[pscustomobject]@{{Ready=$true; Detail='private ACL'}} | ConvertTo-Json -Compress
"""
    try:
        result = run(_powershell_command(script))
        if result.returncode != 0:
            return False, result.stderr.strip() or "ACL command failed"
        payload = json.loads(result.stdout)
    except (OSError, json.JSONDecodeError) as error:
        return False, f"ACL probe unavailable: {type(error).__name__}"
    if not isinstance(payload, dict):
        return False, "ACL response is invalid"
    return payload.get("Ready") is True, str(payload.get("Detail", "ACL response is invalid"))


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in _walk_private_tree(root):
        if path.is_file():
            files.append(path)
    return files


def _safe_archive_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupAuthenticationError("backup member path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupAuthenticationError("backup member path escapes the restore root")
    return path


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(), 0o600)
    try:
        _write_descriptor(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_descriptor(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("private file write made no progress")
        view = view[written:]


def _binary_flag() -> int:
    return int(getattr(os, "O_BINARY", 0))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_exact(stream: Any, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not isinstance(chunk, bytes) or not chunk:
            raise BackupAuthenticationError("backup is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
