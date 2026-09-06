from __future__ import annotations

import errno
import os
import threading
from importlib import import_module
from pathlib import Path
from typing import Any, BinaryIO

from .errors import RuntimeOwnershipError

_lock_module: Any = import_module("msvcrt" if os.name == "nt" else "fcntl")


class ProcessLock:
    """Non-blocking cross-process lock released automatically when the process exits."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle: BinaryIO | None = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Acquire ownership and return True, or False if this object already owns it."""
        with self._mutex:
            if self._handle is not None:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            acquired = False
            try:
                # ``a+b`` leaves the CRT file position at EOF for an existing
                # lock file.  msvcrt.locking() starts at that position, so
                # normalize it before taking the one-byte ownership lock;
                # otherwise a later acquisition would lock byte 1 and release
                # byte 0, leaking the real lock on Windows.
                handle.seek(0)
                try:
                    if os.name == "nt":
                        _lock_module.locking(handle.fileno(), _lock_module.LK_NBLCK, 1)
                    else:
                        _lock_module.flock(
                            handle.fileno(),
                            _lock_module.LOCK_EX | _lock_module.LOCK_NB,
                        )
                except OSError as error:
                    if not self._is_lock_contention(error):
                        raise
                    raise RuntimeOwnershipError(
                        f"another process owns runtime lock: {self.path}"
                    ) from error
                # On Windows, a held byte-range lock can make even a read of
                # that byte fail with a generic EACCES.  Lock before probing
                # or initializing the file so every ownership collision is
                # reported by the lock syscall, where it can be classified
                # precisely.
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                acquired = True
            finally:
                if not acquired:
                    handle.close()
            self._handle = handle
            return True

    @staticmethod
    def _is_lock_contention(error: OSError) -> bool:
        if os.name == "nt":
            # ``msvcrt.locking`` may surface a byte-range collision as a
            # generic EACCES/PermissionError without preserving Win32 error
            # 33 (sharing violation).  This check is only used around the
            # lock syscall, so EACCES is an ownership signal here while
            # unrelated file setup/read/write errors remain untouched.
            return getattr(error, "winerror", None) == 33 or error.errno == errno.EACCES
        return error.errno in {errno.EACCES, errno.EAGAIN}

    def release(self) -> None:
        with self._mutex:
            handle = self._handle
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    _lock_module.locking(handle.fileno(), _lock_module.LK_UNLCK, 1)
                else:
                    _lock_module.flock(
                        handle.fileno(),
                        _lock_module.LOCK_UN,
                    )
            finally:
                handle.close()
                self._handle = None
