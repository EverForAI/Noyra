"""Bounded SMTP workers and cancellation without blocking the shared executor."""

from __future__ import annotations

import asyncio
import queue
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import copy_context
from typing import Any

_WORKERS = threading.BoundedSemaphore(4)


class SMTPOperation:
    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.cancelled = threading.Event()
        self.client: Any = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("SMTP operation deadline or cancellation")
        return remaining

    def abort(self) -> None:
        self.cancelled.set()
        # close() on smtplib's buffered reader may wait for its read lock.
        # Socket shutdown interrupts read/write without taking that lock.
        sock = getattr(self.client, "sock", None)
        shutdown = getattr(sock, "shutdown", None)
        if callable(shutdown):
            with suppress(OSError):
                shutdown(socket.SHUT_RDWR)

    async def run(self, function: Callable[[], str]) -> str:
        while not _WORKERS.acquire(blocking=False):
            await asyncio.sleep(min(0.01, self.remaining()))
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        context = copy_context()

        def invoke() -> None:
            try:
                self.remaining()
                outcome.put((True, context.run(function)))
            except BaseException as error:
                outcome.put((False, error))
            finally:
                _WORKERS.release()

        worker = threading.Thread(target=invoke, name="noyra-smtp", daemon=True)
        try:
            worker.start()
        except BaseException:
            _WORKERS.release()
            raise
        try:
            while True:
                try:
                    succeeded, result = outcome.get_nowait()
                    if succeeded:
                        return str(result)
                    raise result
                except queue.Empty:
                    await asyncio.sleep(min(0.01, self.remaining()))
        except BaseException:
            self.abort()
            raise
