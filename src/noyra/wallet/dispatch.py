"""Final wallet dispatch boundaries shared by policy writers and network workers."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any

from noyra.core.admission import current_commit_scope
from noyra.core.database import Database
from noyra.core.deadline import monotonic_time
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.http import SyncHTTPTimeoutHook
from noyra.core.locking import ProcessLock

DispatchScope = Callable[[], AbstractContextManager[None]]
_DISPATCH_SCOPE: ContextVar[DispatchScope | None] = ContextVar(
    "wallet_dispatch_scope", default=None
)


class WalletDispatchDenied(RuntimeError):
    """This attempt has not sent payment bytes and no longer has authorization."""


@contextmanager
def wallet_policy_scope(database: Database) -> Iterator[None]:
    # One stable file per database, shared across store instances and processes.
    # Order: runtime gate -> process lock -> short DB read/write. Never retain a
    # SQLite transaction during external I/O, or acquire the gate from a worker
    # while a caller holds it waiting for that worker.
    lock = ProcessLock(database.path.with_name(database.path.name + ".wallet-dispatch.lock"))
    deadline = monotonic_time() + 65
    with current_commit_scope():
        while True:
            try:
                lock.acquire()
                break
            except RuntimeOwnershipError:
                if monotonic_time() >= deadline:
                    raise WalletDispatchDenied("wallet dispatch boundary is busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            lock.release()


@contextmanager
def bind_wallet_dispatch(scope: DispatchScope) -> Iterator[None]:
    token = _DISPATCH_SCOPE.set(scope)
    try:
        yield
    finally:
        _DISPATCH_SCOPE.reset(token)


class WalletRequestFence:
    """Hold final authority only in the thread actually writing payment bytes."""

    def __init__(self, deadline: float, hook: SyncHTTPTimeoutHook, *, payment: bool):
        self.deadline = deadline
        self.hook = hook
        self.scope = _DISPATCH_SCOPE.get() if payment else None
        self.payment = payment
        self.stack = ExitStack()

    def check_deadline(self) -> None:
        self.hook.assert_active()
        if monotonic_time() >= self.deadline:
            raise TimeoutError("wallet request deadline exceeded")

    def trace(self, event: str, info: dict[str, Any]) -> None:
        if event.endswith("send_request_headers.started"):
            self.check_deadline()
            if self.payment:
                self.stack.enter_context(self.scope() if self.scope else current_commit_scope())
            self.check_deadline()
        elif event.endswith("send_request_body.started"):
            self.check_deadline()
        elif event.endswith(
            (
                "send_request_body.complete",
                "send_request_body.failed",
                "send_request_headers.failed",
            )
        ):
            self.close()

    def close(self) -> None:
        self.stack.close()
