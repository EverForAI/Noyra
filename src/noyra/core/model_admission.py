"""Notify runtime accounting after reservation, before an admitted attempt starts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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


_PREPARED: ContextVar[tuple[Callable[[Any, Any], None], ...]] = ContextVar(
    "noyra_model_prepared_callbacks", default=()
)


@contextmanager
def track_model_preparation(callback: Callable[[Any, Any], None]) -> Iterator[None]:
    """Bind business evidence in the existing model-call preparation transaction.

    Callbacks must use the supplied connection and perform no external work.
    Default behavior is unchanged; nested trackers retain outer observers.
    """
    token = _PREPARED.set((*_PREPARED.get(), callback))
    try:
        yield
    finally:
        _PREPARED.reset(token)


def notify_model_preparation(connection: Any, call: Any) -> None:
    for callback in _PREPARED.get():
        callback(connection, call)
