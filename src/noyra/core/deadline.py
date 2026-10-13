"""One monotonic budget propagated across nested synchronous request work."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("noyra_operation_deadline", default=None)


def monotonic_time() -> float:
    """High-resolution monotonic clock for synchronous absolute deadlines.

    Windows ``time.monotonic`` may be backed by a ~15 ms clock.  Absolute
    network deadlines use QPC-backed ``perf_counter``. Asyncio absolute
    scheduling deadlines remain in the event loop's own clock domain.
    """
    return time.perf_counter()


def remaining_seconds(default: float) -> float:
    deadline = _DEADLINE.get()
    remaining = default if deadline is None else min(default, deadline - monotonic_time())
    if remaining <= 0:
        raise TimeoutError("operation deadline exceeded")
    return remaining


@contextmanager
def operation_deadline(seconds: float) -> Iterator[None]:
    deadline = monotonic_time() + remaining_seconds(seconds)
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
