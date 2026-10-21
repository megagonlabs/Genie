"""Exception types that Genie memory operations raise."""


class GenieError(Exception):
    """Base exception for Genie errors. `AccessDenied` is a `PermissionError`, not a subclass of this class."""


class MemoryNotFound(GenieError):
    """Genie raises this error when a requested memory block or link does not exist."""


class MemoryConflict(GenieError):
    """Genie raises this error when a write breaks a memory structure rule, or a concurrent change makes the write stale."""


class UnsupportedOperation(GenieError):
    """Genie raises this error when a backend does not support a requested capability."""


class HookRecursionError(GenieError):
    """Genie raises this error when nested hook operations go above the configured depth."""


class BackendError(GenieError):
    """Base class for backend failures that are not memory conflicts.

    Attributes:
        retryable: If `True`, the same request can succeed when it runs again
            with no change.
    """

    retryable = False


class BackendBusy(BackendError):
    """A backend raises this error when contention goes above the configured wait time.

    Attributes:
        retryable: Always `True`. The same request can succeed after the
            contention ends.
    """

    retryable = True


class BackendUnavailable(BackendError):
    """A backend raises this error when it cannot reach or use its storage now.

    Attributes:
        retryable: Always `True`. The same request can succeed after the
            storage is available again.
    """

    retryable = True


class BackendReadOnly(BackendError):
    """A backend raises this error when a write goes to read-only backend storage."""


class BackendCapacityExceeded(BackendError):
    """A backend raises this error when it has no more space, or another storage limit is reached."""


class BackendCorruption(BackendError):
    """A backend raises this error when its data or storage is corrupt, or is not a database."""
