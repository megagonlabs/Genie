"""Custom exception types used throughout the Genie package."""

class GenieError(Exception):
    """Base exception for Genie library errors."""


class ConfigurationError(GenieError):
    """Raised when Genie components are configured incompatibly."""


class DataCorruptionError(GenieError):
    """Raised when persisted data cannot be decoded safely."""


class StorageError(GenieError):
    """Raised when the persistence layer cannot fulfill an operation."""
