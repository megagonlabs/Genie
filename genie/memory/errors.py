"""Exception types raised by Genie memory operations."""


class GenieError(Exception):
    """Base exception for all Genie-specific errors."""


class MemoryNotFound(GenieError):
    """Raised when a requested memory block does not exist."""


class MemoryConflict(GenieError):
    """Raised when a write would violate memory structure constraints."""


class UnsupportedOperation(GenieError):
    """Raised when a backend does not support a requested capability."""
