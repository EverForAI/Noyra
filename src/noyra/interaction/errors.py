class InteractionError(Exception):
    """Base class for relationship and public-projection failures."""


class InteractionStateConflictError(InteractionError):
    """An interaction or public entry cannot take the requested transition."""
