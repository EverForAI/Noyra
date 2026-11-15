"""Private bounded journal I/O under the existing backup rotation lock."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .at_rest import (
    _fsync_directory,
    _windows_permission_audit,
    _write_descriptor,
)
from .backup_keyring_paths import BackupRotationLocation, _effective_uid, _identity, _revision
from .backup_rotation_records import (
    MAX_ENTRIES,
    MAX_INTENT_BYTES,
    MAX_METADATA_BYTES,
    RotationJournalError,
    RotationPendingRecoveryUnknownError,
    RotationPreparationUnknownError,
    sha256,
)
from .locking import ProcessLock

MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PENDING_ENTRIES = 128
_ENTRY = re.compile(r"[a-f0-9]{64}\.(?:(?:intent|accepted)\.json|accept\.tmp)")
_PENDING = re.compile(r"\.pending-[a-f0-9]{32}")


@dataclass(frozen=True)
class _DirectorySnapshot:
    root_revision: tuple[int, ...]
    metadata: dict[str, os.stat_result]
    pending: dict[str, os.stat_result]
    pending_fingerprint: str | None
    pending_bytes: int


def supported_preparation() -> bool:
    return os.name == "posix" and all(
        hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "fchmod", "link")
    )


class RotationStorage:
    def __init__(self, location: BackupRotationLocation, lock: ProcessLock | None = None):
        self.location, self.lock = location, lock
        self.root = location.journal_path
        self._root: tuple[int, ...] | None = None
        self._snapshot: _DirectorySnapshot | None = None
        self._pending_identities: frozenset[tuple[int, int]] = frozenset()

    @property
    def pending_count(self) -> int:
        return len(self._snapshot.pending) if self._snapshot is not None else 0

    @property
    def pending_bytes(self) -> int:
        return self._snapshot.pending_bytes if self._snapshot is not None else 0

    @property
    def pending_fingerprint(self) -> str | None:
        return self._snapshot.pending_fingerprint if self._snapshot is not None else None

    def _directory(self) -> os.stat_result | None:
        self.location.validate(audit_windows=False)
        try:
            value = self.root.lstat()
        except FileNotFoundError:
            if self._root is not None:
                raise RotationJournalError("rotation journal directory identity changed") from None
            return None
        if (
            not stat.S_ISDIR(value.st_mode)
            or getattr(value, "st_file_attributes", 0) & 0x400
            or value.st_dev != self.location.target.parent.stat().st_dev
        ):
            raise RotationJournalError("rotation journal must be an ordinary same-device directory")
        if os.name == "posix" and (value.st_uid != _effective_uid() or value.st_mode & 0o077):
            raise RotationJournalError("rotation journal directory owner or permissions are unsafe")
        identity = (*_identity(value), value.st_mode, value.st_uid, value.st_gid)
        if self._root is not None and identity != self._root:
            raise RotationJournalError("rotation journal directory identity changed")
        self._root = identity
        return value

    def _require_lock(self) -> None:
        if (
            self.lock is None
            or not self.lock.held
            or self.lock.path != self.location.lock_path
            or not supported_preparation()
        ):
            raise RotationJournalError(
                "rotation preparation requires owned lock and POSIX durability"
            )

    def ensure(self) -> None:
        self._require_lock()
        if self._directory() is None:
            self.root.mkdir(mode=0o700)
        else:
            self.catalog()
            self.require_no_pending()
        if self._directory() is None:
            raise RotationJournalError("rotation journal directory disappeared")
        # Directory creation and its parent's entry must precede any intent.
        _fsync_directory(self.root)
        _fsync_directory(self.root.parent)
        self._directory()

    def path(self, name: str) -> Path:
        if (
            name != "header.json"
            and _ENTRY.fullmatch(name) is None
            and _PENDING.fullmatch(name) is None
        ):
            raise RotationJournalError("rotation journal entry name is invalid or incomplete")
        return self.root / name

    @staticmethod
    def _ordinary_file(value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise RotationJournalError("rotation journal requires regular non-linked files")
        if os.name == "posix" and (value.st_uid != _effective_uid() or value.st_mode & 0o077):
            raise RotationJournalError("rotation journal file owner or permissions are unsafe")
        _identity(value)

    def _file(
        self,
        path: Path,
        *,
        pending_identities: frozenset[tuple[int, int]] = frozenset(),
    ) -> os.stat_result:
        value = path.lstat()
        self._ordinary_file(value)
        if value.st_nlink != 1 and not (
            value.st_nlink == 2 and _identity(value) in pending_identities
        ):
            # A crash after create-only receipt linking can leave exactly its
            # two private, hash-bound aliases. No unrelated hard links qualify.
            if path.name.endswith(".accepted.json"):
                peer = path.with_name(path.name.replace(".accepted.json", ".accept.tmp"))
            elif path.name.endswith(".accept.tmp"):
                peer = path.with_name(path.name.replace(".accept.tmp", ".accepted.json"))
            else:
                raise RotationJournalError("rotation journal requires non-hard-linked files")
            try:
                other = peer.lstat()
            except OSError as error:
                raise RotationJournalError("rotation receipt alias is unsafe") from error
            if (
                value.st_nlink != 2
                or other.st_nlink != 2
                or not stat.S_ISREG(other.st_mode)
                or _identity(value) != _identity(other)
            ):
                raise RotationJournalError("rotation receipt alias is unsafe")
        return value

    def _inventory(self) -> _DirectorySnapshot | None:
        before = self._directory()
        if before is None:
            self._snapshot = None
            self._pending_identities = frozenset()
            return None
        metadata: dict[str, os.stat_result] = {}
        names: list[str] = []
        total = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                if len(names) >= MAX_ENTRIES + MAX_PENDING_ENTRIES:
                    raise RotationJournalError("rotation journal exceeds its entry budget")
                name = entry.name
                if (
                    name != "header.json"
                    and _ENTRY.fullmatch(name) is None
                    and _PENDING.fullmatch(name) is None
                ):
                    raise RotationJournalError(
                        "rotation journal entry name is invalid or incomplete"
                    )
                value = self.path(name).lstat()
                metadata[name] = value
                names.append(name)
                total += value.st_size
                if total > MAX_TOTAL_BYTES:
                    raise RotationJournalError("rotation journal exceeds its total byte budget")

        pending = {name: metadata[name] for name in names if _PENDING.fullmatch(name)}
        committed = {name: metadata[name] for name in names if name not in pending}
        if len(committed) > MAX_ENTRIES:
            raise RotationJournalError("rotation journal exceeds its entry budget")
        if len(pending) > MAX_PENDING_ENTRIES:
            raise RotationJournalError("rotation journal exceeds its pending-entry budget")
        for value in pending.values():
            self._ordinary_file(value)
            if not 0 <= value.st_size <= MAX_INTENT_BYTES:
                raise RotationJournalError("rotation pending entry exceeds its byte budget")
            if value.st_nlink == 2:
                peers = [peer for peer in committed.values() if _identity(peer) == _identity(value)]
                if len(peers) != 1 or peers[0].st_nlink != 2:
                    raise RotationJournalError("rotation pending hard-link alias is unsafe")
                self._ordinary_file(peers[0])
            elif value.st_nlink != 1:
                raise RotationJournalError("rotation pending hard-link count is unsafe")

        if os.name == "nt":
            ready, _ = _windows_permission_audit(
                self.root, max_entries=MAX_ENTRIES + MAX_PENDING_ENTRIES + 1
            )
            if not ready:
                raise RotationJournalError("rotation journal ACL is unsafe or unavailable")
        root_after = self._directory()
        if root_after is None or _revision(before) != _revision(root_after):
            raise RotationJournalError("rotation journal changed during inspection; retry")
        for name, value in pending.items():
            if _revision(self.path(name).lstat()) != _revision(value):
                raise RotationJournalError("rotation pending entry changed during inspection")
        pending_bytes = sum(value.st_size for value in pending.values())
        fingerprint = None
        if pending:
            encoded = json.dumps(
                {
                    "root": _revision(before),
                    "pending": [
                        [name, _revision(value)] for name, value in sorted(pending.items())
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            fingerprint = sha256(encoded)
        return _DirectorySnapshot(_revision(before), metadata, pending, fingerprint, pending_bytes)

    def require_no_pending(self) -> None:
        if self.pending_count:
            raise RotationJournalError("rotation pending temporary files require explicit recovery")

    @staticmethod
    def _limit(name: str) -> int:
        if name.endswith(".intent.json"):
            return MAX_INTENT_BYTES
        return MAX_METADATA_BYTES

    def read(self, name: str) -> bytes:
        if self._directory() is None:
            raise RotationJournalError("rotation journal directory is unavailable")
        path = self.path(name)
        before = self._file(path, pending_identities=self._pending_identities)
        limit = self._limit(name)
        if not (0 <= before.st_size <= limit) or (not before.st_size and not name.endswith(".tmp")):
            raise RotationJournalError("rotation journal entry exceeds its byte budget")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
        try:
            if _revision(os.fstat(descriptor)) != _revision(before):
                raise RotationJournalError("rotation journal file changed before reading")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if _revision(os.fstat(descriptor)) != _revision(before):
                raise RotationJournalError("rotation journal file changed while reading")
        finally:
            os.close(descriptor)
        self._directory()
        current = self._file(path, pending_identities=self._pending_identities)
        if _revision(current) != _revision(before):
            raise RotationJournalError("rotation journal file changed after reading")
        raw = b"".join(chunks)
        if len(raw) > limit or (not raw and not name.endswith(".tmp")):
            raise RotationJournalError("rotation journal entry exceeds its byte budget")
        return raw

    def catalog(self) -> dict[str, bytes] | None:
        snapshot = self._inventory()
        if snapshot is None:
            return None
        pending_identities = frozenset(_identity(value) for value in snapshot.pending.values())
        self._snapshot = snapshot
        self._pending_identities = pending_identities
        names = sorted(name for name in snapshot.metadata if name not in snapshot.pending)
        values: dict[str, bytes] = {}
        for name in names:
            value = self._file(self.path(name), pending_identities=pending_identities)
            if _revision(value) != _revision(snapshot.metadata[name]):
                raise RotationJournalError("rotation journal changed during inspection; retry")
            values[name] = self.read(name)
        after = self._inventory()
        if (
            after is None
            or snapshot.root_revision != after.root_revision
            or snapshot.pending_fingerprint != after.pending_fingerprint
        ):
            raise RotationJournalError("rotation journal changed during inspection; retry")
        self._snapshot = after
        self._pending_identities = frozenset(_identity(value) for value in after.pending.values())
        return values

    def recover_pending(self, expected_fingerprint: str) -> tuple[int, int]:
        self._require_lock()
        values = self.catalog()
        if values is None:
            raise RotationJournalError("rotation journal is unavailable")
        if not self.pending_count or self.pending_fingerprint != expected_fingerprint:
            raise RotationJournalError("rotation pending fingerprint changed; inspect again")
        snapshot = self._snapshot
        assert snapshot is not None
        removed_count, removed_bytes = len(snapshot.pending), snapshot.pending_bytes
        for name, expected in sorted(snapshot.pending.items()):
            try:
                descriptor = os.open(
                    self.path(name),
                    os.O_RDONLY
                    | int(getattr(os, "O_NOFOLLOW", 0))
                    | int(getattr(os, "O_NONBLOCK", 0)),
                )
                try:
                    opened = os.fstat(descriptor)
                    current = self.path(name).lstat()
                    if _revision(opened) != _revision(expected) or _revision(current) != _revision(
                        expected
                    ):
                        raise RotationJournalError("rotation pending entry changed before recovery")
                    self._ordinary_file(opened)
                    if opened.st_nlink not in {1, 2}:
                        raise RotationJournalError("rotation pending hard-link count is unsafe")
                    if opened.st_nlink == 2:
                        peers = [
                            peer
                            for peer in snapshot.metadata
                            if peer not in snapshot.pending
                            and _identity(snapshot.metadata[peer]) == _identity(opened)
                        ]
                        if len(peers) != 1:
                            raise RotationJournalError("rotation pending alias is unsafe")
                        peer = peers[0]
                        peer_path = self.path(peer)
                        peer_current = self._file(
                            peer_path, pending_identities=frozenset({_identity(opened)})
                        )
                        if (
                            _revision(peer_current) != _revision(snapshot.metadata[peer])
                            or peer_current.st_nlink != 2
                        ):
                            raise RotationJournalError(
                                "rotation pending alias changed before recovery"
                            )
                    self._directory()
                    self.path(name).unlink()
                finally:
                    os.close(descriptor)
            except (OSError, RotationJournalError) as error:
                raise RotationPendingRecoveryUnknownError() from error
        try:
            _fsync_directory(self.root)
            remaining = self.catalog()
            if remaining is None or self.pending_count:
                raise RotationPendingRecoveryUnknownError()
        except (OSError, RotationJournalError) as error:
            if isinstance(error, RotationPendingRecoveryUnknownError):
                raise
            raise RotationPendingRecoveryUnknownError() from error
        return removed_count, removed_bytes

    def _cleanup(self, temporary: Path, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            if self._directory() is None:
                return
            value = temporary.lstat()
            if stat.S_ISREG(value.st_mode) and _identity(value) == identity:
                temporary.unlink()
        except (OSError, ValueError, RotationJournalError):
            return

    def create(self, name: str, raw: bytes, *, request_id: str) -> None:
        self._require_lock()
        if self._directory() is None:
            raise RotationJournalError("rotation journal directory is unavailable")
        target = self.path(name)
        if target.exists() or target.is_symlink():
            raise RotationJournalError("rotation journal immutable entry already exists")
        limit = self._limit(name)
        if not raw or len(raw) > limit:
            raise RotationJournalError("rotation journal entry exceeds its byte budget")
        existing = self.catalog()
        if (
            existing is None
            or len(existing) >= MAX_ENTRIES
            or (sum(len(value) for value in existing.values()) + len(raw) > MAX_TOTAL_BYTES)
        ):
            raise RotationJournalError("rotation journal new entry exceeds its work or byte budget")
        self.require_no_pending()
        # Temporary names are never accepted by the reader as committed records.
        temporary = self.root / (".pending-" + os.urandom(16).hex())
        identity = None
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
            )
            identity = _identity(os.fstat(descriptor))
            chmod = getattr(os, "fchmod", None)
            assert callable(chmod)
            chmod(descriptor, 0o600)
            _write_descriptor(descriptor, raw)
            os.fsync(descriptor)
            completed_descriptor, descriptor = descriptor, None
            os.close(completed_descriptor)
            self._directory()
            if _identity(self._file(temporary)) != identity:
                raise RotationJournalError("rotation journal temporary identity changed")
            # link is create-only; even a concurrent unexpected final entry is
            # never overwritten. Remove our known temporary link before sync.
            os.link(temporary, target, follow_symlinks=False)
            self._directory()
            value = temporary.lstat()
            if not stat.S_ISREG(value.st_mode) or _identity(value) != identity:
                raise RotationJournalError("rotation journal temporary identity changed")
            temporary.unlink()
            _fsync_directory(self.root)
            if self.read(name) != raw:
                raise RotationJournalError("rotation journal publication readback conflicts")
        except OSError as error:
            raise RotationPreparationUnknownError(request_id) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._cleanup(temporary, identity)

    def write_stage(self, name: str, raw: bytes) -> None:
        """Reuse only a named, validated prefix of the same immutable intent."""
        self._require_lock()
        if not name.endswith(".accept.tmp"):
            raise RotationJournalError("rotation staging name is invalid")
        if not raw or len(raw) > self._limit(name):
            raise RotationJournalError("rotation staging exceeds its byte budget")
        existing = self.catalog()
        if existing is None:
            raise RotationJournalError("rotation journal is unavailable")
        self.require_no_pending()
        path = self.path(name)
        previous = existing.get(name)
        if previous is not None and not raw.startswith(previous):
            raise RotationJournalError("rotation staging content conflicts")
        if (
            sum(len(data) for key, data in existing.items() if key != name) + len(raw)
            > MAX_TOTAL_BYTES
        ):
            raise RotationJournalError("rotation staging exceeds its byte budget")
        if previous is None and len(existing) >= MAX_ENTRIES:
            raise RotationJournalError("rotation staging exceeds its entry budget")
        expected = self._file(path) if previous is not None else None
        readonly = previous == raw
        flags = os.O_RDONLY if readonly else os.O_RDWR
        flags |= int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_NONBLOCK", 0))
        if expected is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            if expected is None:
                chmod = getattr(os, "fchmod", None)
                assert callable(chmod)
                chmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            current = self._file(path)
            if (
                (not readonly and opened.st_nlink != 1)
                or _identity(opened) != _identity(current)
                or (expected is not None and _revision(opened) != _revision(expected))
            ):
                raise RotationJournalError("rotation staging identity changed before writing")
            self._directory()
            if not readonly:
                os.ftruncate(descriptor, 0)
                _write_descriptor(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._directory()
        if self.read(name) != raw:
            raise RotationJournalError("rotation staging readback conflicts")
        _fsync_directory(self.root)

    def link_acceptance(self, stage: str, destination: str, raw: bytes) -> None:
        self._require_lock()
        if not stage.endswith(".accept.tmp") or destination != stage.replace(
            ".accept.tmp", ".accepted.json"
        ):
            raise RotationJournalError("rotation acceptance staging name conflicts")
        values = self.catalog()
        if values is None or self.read(stage) != raw:
            raise RotationJournalError("rotation acceptance staging conflicts")
        self.require_no_pending()
        if (
            len(values) >= MAX_ENTRIES
            or sum(len(data) for data in values.values()) + len(raw) > MAX_TOTAL_BYTES
        ):
            raise RotationJournalError("rotation acceptance exceeds its work or byte budget")
        before = self._file(self.path(stage))
        self._directory()
        os.link(self.path(stage), self.path(destination), follow_symlinks=False)
        if _identity(self._file(self.path(destination))) != _identity(before):
            raise RotationJournalError("rotation acceptance publication identity changed")

    def remove_stage(self, name: str, raw: bytes) -> None:
        self._require_lock()
        self.catalog()
        self.require_no_pending()
        if not name.endswith(".accept.tmp") or self.read(name) != raw:
            raise RotationJournalError("rotation staging cleanup conflicts")
        expected = self._file(self.path(name))
        self._directory()
        if _revision(self._file(self.path(name))) != _revision(expected):
            raise RotationJournalError("rotation staging changed before cleanup")
        self.path(name).unlink()
        _fsync_directory(self.root)

    def confirm(self, name: str) -> None:
        self._require_lock()
        self.catalog()
        self.require_no_pending()
        if self._directory() is None:
            raise RotationJournalError("rotation journal directory is unavailable")
        before = self._file(self.path(name))
        descriptor = os.open(
            self.path(name),
            os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_NONBLOCK", 0)),
        )
        try:
            if _revision(os.fstat(descriptor)) != _revision(before):
                raise RotationJournalError("rotation journal file changed before synchronization")
            os.fsync(descriptor)
            if _revision(os.fstat(descriptor)) != _revision(before):
                raise RotationJournalError("rotation journal file changed during synchronization")
        finally:
            os.close(descriptor)
        self._directory()
        _fsync_directory(self.root)
        if _revision(self._file(self.path(name))) != _revision(before):
            raise RotationJournalError("rotation journal file changed after synchronization")
