class WorldError(Exception):
    """Base class for world-observation failures."""


class UnsafeSourceError(WorldError):
    """A source URL or resolved address violates the read-only network boundary."""


class FetchError(WorldError):
    """A sanitized external fetch failure."""


class WorldStateConflictError(WorldError):
    """A world-state revision conflicts with durable state."""


class GenesisProtocolError(WorldError):
    """A genesis transition or cycle violates the protocol."""
