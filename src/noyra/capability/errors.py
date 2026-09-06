class CapabilityError(Exception):
    """Base class for grant and tool-boundary failures."""


class CapabilityDeniedError(CapabilityError):
    """No active grant authorizes a requested resource or side effect."""


class CapabilityConflictError(CapabilityError):
    """A grant or usage record conflicts with durable state."""
