class SleepError(Exception):
    """Base class for fatigue and sleep protocol failures."""


class SleepStateConflictError(SleepError):
    """A sleep transition or integration conflicts with durable state."""


class FatigueStateError(SleepError):
    """Fatigue state is missing or inconsistent."""
