"""Bounded in-process monotonic cooldowns, reconstructed from UTC after restart."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

from .deadline import monotonic_time
from .errors import IntegrityError


def aware_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("naive time")
        return result.astimezone(UTC)
    except (TypeError, ValueError) as error:
        raise IntegrityError("scheduler timestamp is invalid") from error


class CooldownClock:
    """No background timers, fake timestamps, or budget-day changes.

    A changed durable token establishes a new deadline. Within one process, moving
    the wall clock does not shorten/extend an already anchored elapsed interval.
    A restarted process conservatively reconstructs only its remaining UTC delay.
    """

    def __init__(self, clock: Callable[[], float] = monotonic_time, *, capacity: int = 256):
        self.clock = clock
        self.capacity = capacity
        self._deadlines: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def ready(self, key: str, token: str, remaining_seconds: float) -> bool:
        now = self.clock()
        saved = self._deadlines.get(key)
        if saved is None or saved[0] != token:
            saved = (token, now + max(0.0, remaining_seconds))
            self._deadlines[key] = saved
        self._deadlines.move_to_end(key)
        while len(self._deadlines) > self.capacity:
            self._deadlines.popitem(last=False)
        return now >= saved[1]

    def defer(self, key: str, token: str, seconds: float) -> None:
        self._deadlines.pop(key, None)
        self.ready(key, token, seconds)
