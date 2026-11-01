"""Notify runtime accounting after reservation, before an admitted attempt starts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ATTEMPT_ADMITTED: ContextVar[Callable[[], None] | None] = ContextVar(
    "noyra_model_attempt_admitted", default=None
)


@contextmanager
def track_model_admission(callback: Callable[[], None]) -> Iterator[None]:
    token = _ATTEMPT_ADMITTED.set(callback)
    try:
        yield
    finally:
        _ATTEMPT_ADMITTED.reset(token)


def notify_model_admission() -> None:
    callback = _ATTEMPT_ADMITTED.get()
    if callback is not None:
        callback()
