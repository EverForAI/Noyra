from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .errors import RuntimeOwnershipError


class OperationInvalidated(RuntimeError):
    """Raised when an operation crosses a lifecycle or shutdown boundary."""


@dataclass(frozen=True)
class OperationLease:
    """A short-lived fencing token for work that may cross an await."""

    subject_id: str
    epoch: int
    operation_id: str
    kind: str
    _gate: RuntimeAdmissionGate
    allow_quarantine: bool = False

    def assert_current(self) -> None:
        self._gate.assert_current(self)

    @property
    def valid(self) -> bool:
        try:
            self.assert_current()
        except OperationInvalidated:
            return False
        return True


class RuntimeAdmissionGate:
    """Thread-safe admission and epoch fencing for one subject runtime.

    The gate is intentionally in-memory. Durable lifecycle versions remain the
    source of truth for restart recovery; this gate closes the check/await/write
    race while one process owns the subject.
    """

    def __init__(
        self,
        subject_id: str,
        *,
        initially_accepting: bool = True,
        ownership_check: Callable[[], bool] | None = None,
        control_ownership_check: Callable[[], bool] | None = None,
        migration_clear_check: Callable[[], bool] | None = None,
    ):
        self.subject_id = subject_id
        self._condition = threading.Condition(threading.RLock())
        self._epoch = 0
        self._active = 0
        self._accepting = initially_accepting
        self._quarantined = False
        self._closed = False
        self._ownership_check = ownership_check
        self._control_ownership_check = control_ownership_check or ownership_check
        self._migration_clear_check = migration_clear_check
        self._migration_fenced = False
        self._migration_fence_epoch: int | None = None
        self._migration_control_active = False

    @property
    def epoch(self) -> int:
        with self._condition:
            return self._epoch

    @property
    def quarantined(self) -> bool:
        with self._condition:
            return self._quarantined

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting and not self._closed and not self._migration_fenced

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def migration_fenced(self) -> bool:
        with self._condition:
            return self._migration_fenced

    @property
    def migration_fence_epoch(self) -> int | None:
        with self._condition:
            return self._migration_fence_epoch

    @property
    def active_operations(self) -> int:
        with self._condition:
            return self._active

    def open(self, *, epoch: int | None = None) -> None:
        with self._condition:
            if self._migration_fenced and (
                self._migration_clear_check is not None
                and not self._migration_clear_check()
            ):
                raise RuntimeOwnershipError("migration epoch is still active")
            if epoch is not None:
                self._epoch = max(self._epoch, int(epoch))
            self._quarantined = False
            self._accepting = True
            self._migration_fenced = False
            self._migration_fence_epoch = None
            self._closed = False
            self._condition.notify_all()

    def quarantine(self) -> int:
        return self.invalidate(quarantined=True)

    def clear_quarantine(self) -> None:
        """Clear a startup quarantine after integrity recovery without opening admission."""
        with self._condition:
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            self._quarantined = False
            self._accepting = False
            self._condition.notify_all()

    def begin_drain(self) -> int:
        return self.invalidate(closed=True)

    def invalidate(self, *, quarantined: bool = False, closed: bool = False) -> int:
        with self._condition:
            self._epoch += 1
            self._accepting = False
            self._quarantined = quarantined or self._quarantined
            self._closed = closed or self._closed
            self._condition.notify_all()
            return self._epoch

    @contextmanager
    def migration_control_scope(self) -> Iterator[None]:
        """Run one authenticated migration control without normal admission bypass.

        Migration controls are allowed while the durable source epoch is active,
        but only the process that owns this runtime may enter the scope.  The
        scope deliberately does not hold the gate lock while the coordinator
        performs network or filesystem work, so ordinary leases can drain.
        """
        with self._condition:
            if self._control_ownership_check is not None and not self._control_ownership_check():
                raise RuntimeOwnershipError("runtime ownership is required for migration control")
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            if self._quarantined:
                raise OperationInvalidated("runtime is in integrity quarantine")
            if self._migration_control_active:
                raise OperationInvalidated("migration control is already active")
            self._migration_control_active = True
        try:
            yield
        finally:
            with self._condition:
                self._migration_control_active = False
                self._condition.notify_all()

    def fence_for_migration(self, timeout: float | None = None) -> int:
        """Invalidate normal leases and wait until all in-flight work drains."""
        with self._condition:
            if not self._migration_control_active:
                raise OperationInvalidated("migration control scope is required to fence runtime")
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            self._epoch += 1
            self._accepting = False
            self._migration_fenced = True
            self._migration_fence_epoch = self._epoch
            self._condition.notify_all()
            fence_epoch = self._epoch
        if not self.wait_for_drain(timeout):
            raise OperationInvalidated("migration fence could not drain active operations")
        return fence_epoch

    def clear_migration_fence(self, fence_epoch: int) -> None:
        """Reopen normal admission after the durable migration epoch is revoked."""
        with self._condition:
            if not self._migration_control_active:
                raise OperationInvalidated("migration control scope is required to clear fence")
            if self._migration_fence_epoch != int(fence_epoch):
                raise OperationInvalidated("migration fence epoch is stale")
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            if self._migration_clear_check is not None and not self._migration_clear_check():
                raise RuntimeOwnershipError("migration epoch is still active")
            self._migration_fenced = False
            self._migration_fence_epoch = None
            self._accepting = not self._quarantined
            self._condition.notify_all()

    def assert_migration_fenced(self) -> None:
        with self._condition:
            if not self._migration_fenced:
                raise OperationInvalidated("migration source is not fenced")

    def begin(self, kind: str, *, allow_quarantine: bool = False) -> OperationLease:
        if not kind or not kind.strip():
            raise ValueError("operation kind is required")
        with self._condition:
            if self._ownership_check is not None and not self._ownership_check():
                raise RuntimeOwnershipError("runtime ownership is required for operation admission")
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            if self._migration_fenced:
                raise RuntimeOwnershipError("runtime is fenced for migration")
            if not self._accepting and not allow_quarantine:
                raise OperationInvalidated("runtime admission is closed")
            if self._quarantined and not allow_quarantine:
                raise OperationInvalidated("runtime is in integrity quarantine")
            lease = OperationLease(
                self.subject_id,
                self._epoch,
                uuid.uuid4().hex,
                kind,
                self,
                allow_quarantine,
            )
            self._active += 1
            return lease

    def finish(self, lease: OperationLease) -> None:
        with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()

    @contextmanager
    def operation(self, kind: str, *, allow_quarantine: bool = False) -> Iterator[OperationLease]:
        lease = self.begin(kind, allow_quarantine=allow_quarantine)
        try:
            yield lease
        finally:
            self.finish(lease)

    @contextmanager
    def commit_scope(self, lease: OperationLease) -> Iterator[None]:
        """Serialize a final durable commit with pause/reset invalidation."""
        with self._condition:
            self._assert_current_locked(lease)
            yield

    @contextmanager
    def external_side_effect_scope(self, lease: OperationLease) -> Iterator[None]:
        """Linearize an irreversible external side effect with lifecycle fencing.

        The gate lock is held only across the provider call boundary. A
        pause/quarantine therefore cannot pass the final admission check and
        then race an outgoing signer request, while the rest of the operation
        remains outside the lock.
        """
        with self._condition:
            self._assert_current_locked(lease)
            yield

    @contextmanager
    def lifecycle_control_scope(self) -> Iterator[None]:
        """Serialize lifecycle controls with integrity quarantine changes."""

        with self._condition:
            if self._closed:
                raise OperationInvalidated("runtime is draining")
            if self._quarantined:
                raise OperationInvalidated("runtime is in integrity quarantine")
            yield

    def assert_current(self, lease: OperationLease) -> None:
        with self._condition:
            self._assert_current_locked(lease)

    def _assert_current_locked(self, lease: OperationLease) -> None:
        if lease.subject_id != self.subject_id:
            raise OperationInvalidated("operation subject mismatch")
        if self._ownership_check is not None and not self._ownership_check():
            raise OperationInvalidated("runtime ownership was released")
        if lease.epoch != self._epoch:
            raise OperationInvalidated("operation epoch is stale")
        if self._closed:
            raise OperationInvalidated("runtime is draining")
        if self._migration_fenced:
            raise OperationInvalidated("runtime is fenced for migration")
        if not self._accepting and not lease.allow_quarantine:
            raise OperationInvalidated("runtime admission is closed")
        if self._quarantined and not lease.allow_quarantine:
            raise OperationInvalidated("runtime is in integrity quarantine")

    def wait_for_drain(self, timeout: float | None = None) -> bool:
        with self._condition:
            if timeout is None:
                while self._active:
                    self._condition.wait()
                return True
            end = time.monotonic() + max(0.0, timeout)
            while self._active:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def require_owned(self, process_lock: object) -> None:
        if not bool(getattr(process_lock, "held", False)):
            raise RuntimeOwnershipError("runtime ownership is required for operation admission")


_CURRENT_LEASE: ContextVar[OperationLease | None] = ContextVar(
    "noyra_current_operation_lease", default=None
)
_ALLOW_UNFENCED_COMMITS: ContextVar[bool] = ContextVar(
    "noyra_allow_unfenced_commits", default=False
)


@contextmanager
def bind_lease(lease: OperationLease) -> Iterator[None]:
    token = _CURRENT_LEASE.set(lease)
    try:
        yield
    finally:
        _CURRENT_LEASE.reset(token)


def assert_current_lease() -> None:
    lease = _CURRENT_LEASE.get()
    if lease is not None:
        lease.assert_current()


def current_lease() -> OperationLease | None:
    return _CURRENT_LEASE.get()


@contextmanager
def accounting_scope() -> Iterator[None]:
    """Allow terminal accounting to settle after an operation is fenced.

    Provider/delivery workers must record an ``unknown`` or terminal outcome
    after a pause or shutdown.  This bypass is deliberately narrow and should
    only surround those accounting transactions, never normal subject state
    commits or new external work.
    """

    token = _ALLOW_UNFENCED_COMMITS.set(True)
    try:
        yield
    finally:
        _ALLOW_UNFENCED_COMMITS.reset(token)


@contextmanager
def current_commit_scope() -> Iterator[None]:
    """Fence the current lease across one short durable transaction."""

    if _ALLOW_UNFENCED_COMMITS.get():
        yield
        return
    lease = _CURRENT_LEASE.get()
    if lease is None:
        yield
        return
    with lease._gate.commit_scope(lease):
        yield
