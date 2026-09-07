from __future__ import annotations

import importlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from noyra.core.types import new_id

_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))
_DIRECTORY = int(getattr(os, "O_DIRECTORY", 0))
_CLOEXEC = int(getattr(os, "O_CLOEXEC", 0))
_NONBLOCK = int(getattr(os, "O_NONBLOCK", 0))


def _windows_long_path(path: Path) -> Path:
    """Expand lexical 8.3 aliases without resolving junctions or symlinks."""
    import ctypes
    from ctypes import wintypes

    ctypes_api: Any = ctypes
    function = ctypes_api.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    suffix: list[str] = []
    while True:
        count = function(str(path), buffer, len(buffer))
        if count:
            if count >= len(buffer):
                raise PermissionError("filesystem grant root exceeds Windows path limits")
            return Path(buffer.value).joinpath(*reversed(suffix))
        error = ctypes_api.get_last_error()
        # A grant may name a not-yet-created directory, but permission and other
        # failures must not be mistaken for an absent suffix.
        if error not in {2, 3} or path.parent == path:
            raise PermissionError("filesystem grant root spelling is unavailable")
        suffix.append(path.name)
        path = path.parent


def normalized_root(value: str) -> Path:
    original = Path(value).expanduser()
    root = Path(os.path.normpath(original))
    if not root.is_absolute():
        raise PermissionError("filesystem grant root must be absolute")
    if os.name == "nt":
        root = _windows_long_path(root)
    if original.resolve() != root:
        raise PermissionError("filesystem grant root has an ambiguous or linked spelling")
    return root


def _trusted_posix_directory(fd: int, *, sticky_ancestor: bool = False) -> None:
    get_euid = getattr(os, "geteuid", None)
    if os.name != "posix" or get_euid is None:
        raise PermissionError("POSIX directory admission is unavailable")
    metadata = os.fstat(fd)
    if metadata.st_uid not in {0, get_euid()}:
        raise PermissionError("filesystem directory is not owned by a trusted account")
    if metadata.st_mode & 0o022 and not (
        sticky_ancestor and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    ):
        raise PermissionError("filesystem directory permits untrusted entry replacement")


@contextmanager
def _parent(path: Path, root: Path, *, create: bool) -> Iterator[tuple[int, Any]]:
    """Walk from the filesystem anchor, never resolving an unchecked ancestor.

    Windows holds non-delete-shared directory handles; POSIX pins each opened
    directory inode. All subsequent I/O is relative to those authorized objects.
    """
    if path != root and root not in path.parents:
        raise PermissionError("file is outside the authorized root")
    handles: list[int] = []
    api: Any = None
    try:
        if os.name == "nt":
            # Reuse the native relative-open implementation used by prototypes.
            from noyra.cognition.execution import _windows_workspace_api

            api = _windows_workspace_api()
            current = api.open_root(Path(path.anchor), share_delete=False)
        elif os.name == "posix" and _NOFOLLOW and _DIRECTORY:
            flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
            current = os.open(path.anchor, flags)
        else:
            raise PermissionError("safe filesystem handles are unavailable")
        handles.append(current)
        location = Path(path.anchor)
        if api is None and create:
            _trusted_posix_directory(current)
        for part in path.parent.parts[1:]:
            location /= part
            can_create = create and (location == root or root in location.parents)
            if api is not None:
                current = api.open_relative(
                    current,
                    part,
                    directory=True,
                    create=can_create,
                    share_delete=False,
                )
            else:
                if can_create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current)
                current = os.open(part, flags, dir_fd=current)
            handles.append(current)
            if api is None and create:
                # The publication namespace must be owner/root controlled. A
                # root-owned sticky /tmp is permitted only above the grant.
                _trusted_posix_directory(current, sticky_ancestor=location in root.parents)
        yield current, api
    finally:
        for handle in reversed(handles):
            if api is not None:
                api.close(handle)
            else:
                os.close(handle)


def _descriptor(api: Any, handle: int, flags: int) -> int:
    try:
        msvcrt = importlib.import_module("msvcrt")
        return int(msvcrt.open_osfhandle(handle, flags | int(getattr(os, "O_BINARY", 0))))
    except BaseException:
        api.close(handle)
        raise


def read_bounded(path: Path, root: Path, limit: int, *, before_read: Callable[[], object]) -> bytes:
    with _parent(path, root, create=False) as (parent, api):
        if api is not None:
            fd = _descriptor(
                api, api.open_relative(parent, path.name, directory=False), os.O_RDONLY
            )
        else:
            fd = os.open(
                path.name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=parent,
            )
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PermissionError("authorized path must be a regular single-link file")
            before_read()
            return stream.read(limit + 1)


def write_atomic(
    path: Path, root: Path, payload: bytes, *, before_publish: Callable[[], object]
) -> None:
    with _parent(path, root, create=True) as (parent, api):
        name = f".{path.name}.{new_id('tmp')}"
        if api is not None:
            handle = api.open_relative(
                parent,
                name,
                directory=False,
                create=True,
                write=True,
                delete=True,
                share_delete=False,
            )
            fd = _descriptor(api, handle, os.O_WRONLY)
        else:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=parent,
            )
        published = False
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("file write made no progress")
                view = view[count:]
            os.fsync(fd)
            before_publish()
            if api is not None:
                msvcrt = importlib.import_module("msvcrt")
                api.rename(msvcrt.get_osfhandle(fd), parent, path.name, replace=True)
            else:
                os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            published = True
        finally:
            try:
                if not published:
                    with suppress(OSError):
                        if api is not None:
                            msvcrt = importlib.import_module("msvcrt")
                            api.delete(msvcrt.get_osfhandle(fd))
                        else:
                            os.unlink(name, dir_fd=parent)
            finally:
                os.close(fd)
