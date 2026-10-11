"""One monotonic budget propagated across nested synchronous request work."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("noyra_operation_deadline", default=None)


def remaining_seconds(default: float) -> float:
    deadline = _DEADLINE.get()
    remaining = default if deadline is None else min(default, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("operation deadline exceeded")
    return remaining


@contextmanager
def operation_deadline(seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + remaining_seconds(seconds)
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
