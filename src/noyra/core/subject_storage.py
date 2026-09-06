from __future__ import annotations

import os
import stat
import threading
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from .identity import validate_subject_id, validate_subject_storage_key


class SubjectStorageDirectory:
    """Resolve and migrate one subject directory without using its logical ID going forward."""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _locks: ClassVar[dict[str, threading.RLock]] = {}

    @classmethod
    def locate(
        cls,
        root: Path | str,
        storage_key: str,
        *,
        legacy_subject_id: str | None = None,
        legacy_migration_allowed: bool = True,
        create: bool = False,
    ) -> Path | None:
        validate_subject_storage_key(storage_key)
        if legacy_subject_id is not None:
            validate_subject_id(legacy_subject_id)
            if storage_key.casefold() == legacy_subject_id.casefold():
                raise ValueError(
                    "subject storage key must be independent of the logical subject id"
                )
        configured_root = Path(root).expanduser()
        if create:
            configured_root.mkdir(parents=True, exist_ok=True)
        try:
            base = configured_root.resolve(strict=True)
        except FileNotFoundError:
            return None
        if not base.is_dir():
            raise ValueError("subject storage root is not a directory")
        lock = cls._lock(base, storage_key)
        with lock:
            return cls._locate_locked(
                base,
                storage_key,
                legacy_subject_id=legacy_subject_id,
                legacy_migration_allowed=legacy_migration_allowed,
                create=create,
            )

    @classmethod
    def _locate_locked(
        cls,
        base: Path,
        storage_key: str,
        *,
        legacy_subject_id: str | None,
        legacy_migration_allowed: bool,
        create: bool,
    ) -> Path | None:
        target = base / storage_key
        legacy = None if legacy_subject_id is None else base / legacy_subject_id
        target_exists = cls._validated_directory(target, base, allow_missing=True)
        legacy_exists = (
            False if legacy is None else cls._validated_directory(legacy, base, allow_missing=True)
        )
        if legacy_exists:
            if legacy is None:
                raise ValueError("legacy subject directory is unavailable")
            if not legacy_migration_allowed:
                raise ValueError("legacy subject workspace is ambiguous on this filesystem")
            if target_exists:
                cls._remove_empty_legacy_or_target(legacy, target)
                target_exists = cls._validated_directory(target, base, allow_missing=True)
                legacy_exists = cls._validated_directory(legacy, base, allow_missing=True)
            if legacy_exists and not target_exists:
                try:
                    os.replace(legacy, target)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ValueError(
                        "legacy subject directory could not be migrated safely"
                    ) from error
                cls._sync_directory(base)
                target_exists = cls._validated_directory(target, base, allow_missing=True)
                legacy_exists = cls._validated_directory(legacy, base, allow_missing=True)
                if legacy_exists or not target_exists:
                    raise ValueError("legacy subject directory migration was not atomic")
        if not target_exists and create:
            with suppress(FileExistsError):
                target.mkdir(mode=0o700)
            cls._sync_directory(base)
            target_exists = cls._validated_directory(target, base, allow_missing=False)
        if not target_exists:
            return None
        cls._validated_directory(target, base, allow_missing=False)
        return target

    @classmethod
    def _remove_empty_legacy_or_target(cls, legacy: Path, target: Path) -> None:
        legacy_empty = cls._directory_is_empty(legacy)
        target_empty = cls._directory_is_empty(target)
        if legacy_empty:
            try:
                legacy.rmdir()
            except OSError as error:
                raise ValueError("empty legacy subject directory could not be removed") from error
            return
        if target_empty:
            try:
                target.rmdir()
            except OSError as error:
                raise ValueError("empty keyed subject directory could not be replaced") from error
            return
        raise ValueError("legacy and keyed subject directories both contain data")

    @staticmethod
    def _directory_is_empty(path: Path) -> bool:
        try:
            next(path.iterdir())
        except StopIteration:
            return True
        except OSError as error:
            raise ValueError("subject directory could not be inspected") from error
        return False

    @classmethod
    def _validated_directory(cls, path: Path, base: Path, *, allow_missing: bool) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise ValueError("subject storage directory is missing") from None
        except OSError as error:
            raise ValueError("subject storage directory could not be inspected") from error
        if not stat.S_ISDIR(metadata.st_mode) or cls._is_link_or_reparse(metadata):
            raise ValueError("subject storage directory cannot be a link or reparse point")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError("subject storage directory could not be resolved") from error
        if resolved.parent != base or resolved.name != path.name:
            raise ValueError("subject storage directory escapes its configured root")
        return True

    @staticmethod
    def _is_link_or_reparse(metadata: os.stat_result) -> bool:
        if stat.S_ISLNK(metadata.st_mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)

    @classmethod
    def _lock(cls, root: Path, storage_key: str) -> threading.RLock:
        key = os.path.normcase(str(root / storage_key))
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            with suppress(OSError):
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
