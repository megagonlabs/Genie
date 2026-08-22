"""Exception types raised by Genie memory operations."""


class GenieError(Exception):
    """Base exception for all Genie-specific errors."""


class MemoryNotFound(GenieError):
    """Raised when a requested memory block or link does not exist."""


class MemoryConflict(GenieError):
    """Raised when a write would violate memory structure constraints."""


class UnsupportedOperation(GenieError):
    """Raised when a backend does not support a requested capability."""


class HookRecursionError(GenieError):
    """Raised when nested hook operations exceed the configured depth."""


class BackendError(GenieError):
    """Base class for backend failures that are not memory conflicts.

    Attributes:
        retryable: Whether retrying the operation may succeed without changing
            the request.
    """

    retryable = False


class BackendBusy(BackendError):
    """Raised when backend contention exceeds the configured wait time.

    Attributes:
        retryable: Always `True`; the same request may succeed once contention
            clears.
    """

    retryable = True


class BackendUnavailable(BackendError):
    """Raised when backend storage cannot currently be reached or used.

    Attributes:
        retryable: Always `True`; the same request may succeed once storage is
            reachable again.
    """

    retryable = True


class BackendReadOnly(BackendError):
    """Raised when a write is attempted against read-only backend storage."""


class BackendCapacityExceeded(BackendError):
    """Raised when backend space or another storage limit is exhausted."""


class BackendCorruption(BackendError):
    """Raised when backend data or storage is corrupt or not a database."""
