from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import noyra.core.locking as locking
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock


def _raising_lock_module(error: OSError) -> SimpleNamespace:
    def fail_lock(*_args: object) -> None:
        raise error

    if os.name == "nt":
        return SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=fail_lock)
    return SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=fail_lock)


def _working_lock_module() -> SimpleNamespace:
    if os.name == "nt":
        return SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=lambda *_args: None,
        )
    return SimpleNamespace(
        LOCK_EX=1,
        LOCK_NB=2,
        LOCK_UN=4,
        flock=lambda *_args: None,
    )


def test_process_lock_propagates_file_setup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = ProcessLock(tmp_path / "unavailable" / "runtime.lock")
    failure = OSError(errno.EIO, "injected lock-directory I/O error")
    original_mkdir = Path.mkdir

    def fail_target_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == lock.path.parent:
            raise failure
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target_mkdir)

    with pytest.raises(OSError) as caught:
        lock.acquire()

    assert caught.value is failure
    assert lock.held is False


def test_process_lock_propagates_post_lock_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = PermissionError(errno.EACCES, "injected lock-file read error")

    class FailingReadHandle:
        closed = False

        def fileno(self) -> int:
            return 1

        def seek(self, _offset: int) -> int:
            return 0

        def read(self, _size: int) -> bytes:
            raise failure

        def close(self) -> None:
            self.closed = True

    handle = FailingReadHandle()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(locking, "_lock_module", _working_lock_module())

    process_lock = ProcessLock(tmp_path / "runtime.lock")
    with pytest.raises(PermissionError) as caught:
        process_lock.acquire()

    assert caught.value is failure
    assert handle.closed is True
    assert process_lock.held is False


def test_process_lock_propagates_non_contention_lock_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = ProcessLock(tmp_path / "runtime.lock")
    failure = OSError(errno.EIO, "injected lock I/O error")
    monkeypatch.setattr(locking, "_lock_module", _raising_lock_module(failure))

    with pytest.raises(OSError) as caught:
        lock.acquire()

    assert caught.value is failure
    assert lock.held is False


def test_process_lock_maps_lock_contention_to_runtime_ownership_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contention: OSError
    if os.name == "nt":
        # Some supported Python/Windows combinations report a real msvcrt
        # collision as PermissionError(errno=EACCES) without winerror=33.
        contention = PermissionError(errno.EACCES, "sharing violation")
    else:
        contention = OSError(errno.EAGAIN, "resource temporarily unavailable")
    monkeypatch.setattr(locking, "_lock_module", _raising_lock_module(contention))

    with pytest.raises(RuntimeOwnershipError) as caught:
        ProcessLock(tmp_path / "runtime.lock").acquire()

    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__ is contention


def test_process_lock_release_propagates_error_and_clears_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = PermissionError(errno.EACCES, "injected unlock error")
    if os.name == "nt":

        def lock_call(*args: object) -> None:
            if args[1] == 2:
                raise failure

        module = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lock_call)
    else:

        def flock_call(*args: object) -> None:
            if args[1] == 4:
                raise failure

        module = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=flock_call)
    monkeypatch.setattr(locking, "_lock_module", module)

    process_lock = ProcessLock(tmp_path / "runtime.lock")
    assert process_lock.acquire() is True

    with pytest.raises(PermissionError) as caught:
        process_lock.release()

    assert caught.value is failure
    assert process_lock.held is False


def test_process_lock_fences_another_process_on_initially_empty_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "cross-process.lock"
    lock_path.touch()
    source_root = Path(__file__).resolve().parents[1] / "src"
    probe = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock

lock = ProcessLock(Path(sys.argv[1]))
try:
    lock.acquire()
except RuntimeOwnershipError:
    raise SystemExit(23)
else:
    lock.release()
"""

    owner = ProcessLock(lock_path)
    assert owner.acquire() is True
    try:
        contender = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path), str(source_root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        owner.release()

    assert contender.returncode == 23, contender.stderr
    successor = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path), str(source_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert successor.returncode == 0, successor.stderr
