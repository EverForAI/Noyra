class MindError(Exception):
    """Base class for causal mind-state failures."""


class CausalValidationError(MindError):
    """A proposed state change lacks valid causal support."""


class MindStateConflictError(MindError):
    """A revision conflicts with the current durable state."""
