class SleepError(Exception):
    """Base class for fatigue and sleep protocol failures."""


class SleepStateConflictError(SleepError):
    """A sleep transition or integration conflicts with durable state."""


class SleepReflectionDeferred(SleepError):
    """A temporarily unavailable model can wait without failing the sleep stage."""


class FatigueStateError(SleepError):
    """Fatigue state is missing or inconsistent."""
