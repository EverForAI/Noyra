class KernelError(Exception):
    """Base class for deterministic kernel errors."""


class IdentityConflictError(KernelError):
    """The requested identity conflicts with an existing identity record."""


class NotFoundError(KernelError):
    """A requested kernel record does not exist."""


class InvalidTransitionError(KernelError):
    """A lifecycle or action state transition is not allowed."""


class DuplicateActionError(KernelError):
    """An action would duplicate an existing idempotent action."""


class EventConflictError(KernelError):
    """An event ID was reused with different immutable content."""


class IntegrityError(KernelError):
    """Persisted content failed its recorded integrity hash."""


class ArchiveKeyUnavailableError(IntegrityError):
    """An encrypted archive cannot be verified because its key is unavailable."""


class ArchiveUnavailableError(IntegrityError):
    """An archive object is temporarily unavailable, so integrity is unknown."""


class ArchiveAuthenticationError(IntegrityError):
    """Encrypted archive bytes failed format or authentication verification."""


class PayloadLimitError(KernelError):
    """A persisted or compressed payload exceeded a caller's explicit bound."""


class RuntimeOwnershipError(KernelError):
    """Another process already owns this subject runtime."""


class TrainingPolicyConflictError(KernelError):
    """The training policy changed before a compare-and-swap update."""
