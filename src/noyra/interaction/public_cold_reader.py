"""Disposable, memory-limited local archive verification; never opens the database."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from noyra.core.archive import ArchiveKeyMaterial, ArchiveKeyring
from noyra.core.database import Database
from noyra.core.deadline import monotonic_time
from noyra.core.errors import (
    ArchiveAuthenticationError,
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import canonical_json, content_hash

MAX_COLD_PROCESS_MEMORY = 512 * 1024 * 1024
MAX_COLD_REPLY_BYTES = 2_000_000


def _memory_limit(maximum: int) -> Any:
    """Return a live Windows job or apply POSIX address-space limit; fail closed."""
    if os.name == "posix":
        import importlib

        resource: Any = importlib.import_module("resource")

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return None
    if os.name != "nt":
        raise OSError("cold reader memory isolation unavailable")
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IOCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # Windows-only ctypes exports are absent from the POSIX type stubs.
    ctypes_api: Any = ctypes
    kernel = ctypes_api.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes_api.WinError(ctypes_api.get_last_error())
    limits = ExtendedLimits()
    # JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
    limits.ProcessMemoryLimit = maximum
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes_api.WinError(ctypes_api.get_last_error())
        kernel.CloseHandle(job)
        raise error
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes_api.WinError(ctypes_api.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return job  # Keep the handle alive until process exit, never close it during work.


class _DirectorySnapshot(Database):
    """Only the parent's pinned namespace rows; never open or initialize a live DB."""

    def __init__(self, path: str, identities: list[str], keys: list[tuple[str, str]]):
        self.path = Path(path)
        self.snapshot = sqlite3.connect(":memory:")
        self.snapshot.row_factory = sqlite3.Row
        self.snapshot.execute("CREATE TABLE subject_identity(subject_id TEXT)")
        self.snapshot.execute("CREATE TABLE subject_storage_keys(subject_id TEXT,storage_key TEXT)")
        self.snapshot.executemany(
            "INSERT INTO subject_identity VALUES(?)", [(i,) for i in identities]
        )
        self.snapshot.executemany("INSERT INTO subject_storage_keys VALUES(?,?)", keys)
        self.snapshot.execute("PRAGMA query_only=ON")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        yield self.snapshot


def _worker(
    channel: Connection, path: str, subject: str, identities: list[str], keys: list[tuple[str, str]]
) -> None:
    try:
        memory_guard = _memory_limit(MAX_COLD_PROCESS_MEMORY)
        database = _DirectorySnapshot(path, identities, keys)
        archive = EventPayloadArchive(
            database,
            database.path.parent / "subject" / "cold",
            subject_id=subject,
            create_root=False,
            read_only_root=True,
            cache_cloud_restores=False,
        )
        keyring = archive.keyring
        channel.send_bytes(
            canonical_json(
                {
                    "kind": "ready",
                    "generation": keyring.generation,
                    "active": keyring.active_key_id,
                    "legacy": keyring.legacy_key_id,
                    "keys": [
                        {**entry, "key": keyring._keys[entry["key_id"]].key.hex()}
                        for entry in keyring.metadata()
                    ],
                }
            ).encode()
        )
        while True:
            command = channel.recv()
            key, subject, metadata, maximum, identifiers = command
            loaded = archive._load_segment(
                subject,
                key,
                connection=None,
                metadata=metadata,
                max_decompressed_bytes=maximum,
                max_events=5_000,
                local_only=True,
            )
            # Authenticate every entry, then return only the approved IDs and
            # state fields. Never send the decoded segment or private bodies.
            states = {}
            for identifier in identifiers:
                payload = loaded.payloads.get(identifier)
                if payload is not None:
                    if (
                        not isinstance(payload.get("from"), str)
                        or not isinstance(payload.get("to"), str)
                        or len(payload["from"]) > 64
                        or len(payload["to"]) > 64
                    ):
                        raise IntegrityError("public lifecycle state invalid")
                    states[identifier] = {
                        "hash": content_hash(payload),
                        "from": payload.get("from"),
                        "to": payload.get("to"),
                    }
            raw = canonical_json(
                {"kind": "result", "bytes": loaded.decompressed_bytes, "states": states}
            ).encode()
            if len(raw) > MAX_COLD_REPLY_BYTES:
                raise PayloadLimitError("public cold projection exceeds reply bound")
            channel.send_bytes(raw)
            del raw, states, loaded
        # The OS reclaims memory_guard with the process (including forced exit).
    except (EOFError, BrokenPipeError):
        return
    except Exception as error:
        # Only an allowlisted type code, never a key/path/body-bearing message.
        with suppress(OSError):
            channel.send_bytes(
                canonical_json({"kind": "error", "type": type(error).__name__}).encode()
            )
    finally:
        channel.close()
        _ = locals().get("memory_guard")


class PublicColdReader:
    def __init__(
        self,
        path: Path,
        subject: str,
        identities: list[str],
        keys: list[tuple[str, str]],
        deadline: float,
    ):
        context = multiprocessing.get_context("spawn")
        self.channel, child = context.Pipe()
        self.process = context.Process(
            target=_worker,
            args=(child, str(path), subject, identities, keys),
            name="noyra-public-cold-reader",
            daemon=True,
        )
        self.deadline = deadline
        self.closed = False
        self.ready = False
        try:
            self.process.start()
        except (OSError, RuntimeError) as error:
            self.close(lambda: None)
            raise ArchiveUnavailableError("public cold reader cannot start") from error
        finally:
            child.close()

    def _receive(self) -> dict[str, Any]:
        import json

        try:
            raw = self.channel.recv_bytes(MAX_COLD_REPLY_BYTES)
        except (EOFError, OSError) as error:
            raise ArchiveUnavailableError("public cold reader exited") from error
        if monotonic_time() >= self.deadline:
            raise TimeoutError("public cold reader deadline exceeded")
        result: dict[str, Any] = json.loads(raw)
        if result["kind"] == "error":
            errors = {
                error.__name__: error
                for error in (
                    IntegrityError,
                    ArchiveUnavailableError,
                    ArchiveAuthenticationError,
                    ArchiveKeyUnavailableError,
                    PayloadLimitError,
                )
            }
            raise errors.get(result["type"], ArchiveUnavailableError)(
                "public cold reader verification failed"
            )
        return result

    def _exchange(self, command: object | None = None) -> dict[str, Any]:
        future: Future[dict[str, Any]] = Future()

        def exchange() -> None:
            try:
                if command is not None:
                    self.channel.send(command)
                future.set_result(self._receive())
            except Exception as error:
                future.set_exception(error)

        remaining = self.deadline - monotonic_time()
        if remaining <= 0:
            raise TimeoutError("public cold reader deadline exceeded")
        threading.Thread(target=exchange, name="noyra-public-cold-ipc", daemon=True).start()
        return future.result(timeout=remaining)

    def prepare(self) -> ArchiveKeyring:
        result = self._exchange()
        if result["kind"] != "ready":
            raise ArchiveUnavailableError("public cold reader initialization failed")
        keyring = ArchiveKeyring(
            generation=result["generation"],
            active_key_id=result["active"],
            legacy_key_id=result["legacy"],
            keys=tuple(
                ArchiveKeyMaterial(
                    entry["key_id"],
                    bytes.fromhex(entry["key"]),
                    entry["fingerprint"],
                    entry["status"],
                )
                for entry in result["keys"]
            ),
        )
        self.ready = True
        return keyring

    def load(
        self, subject: str, key: str, metadata: dict[str, Any], maximum: int, identifiers: list[str]
    ) -> tuple[dict[str, dict[str, Any]], int]:
        if not self.ready:
            raise RuntimeError("public cold reader key revision not validated")
        result = self._exchange((key, subject, metadata, maximum, identifiers))
        if result["kind"] != "result":
            raise ArchiveUnavailableError("public cold reader response invalid")
        return result["states"], result["bytes"]

    def close(self, release: Callable[[], None]) -> None:
        if self.closed:
            return
        self.closed = True
        self.channel.close()
        if self.process.pid is None:
            self.process.close()
            release()
            return
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=0.1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=0.1)
        if not self.process.is_alive():
            self.process.close()
            release()
            return

        def reap() -> None:
            # Kernel-uninterruptible I/O may delay reaping. Keep admission held
            # until that work is actually gone, rather than overbooking memory.
            self.process.join()
            self.process.close()
            release()

        threading.Thread(target=reap, name="noyra-public-cold-reaper", daemon=True).start()
