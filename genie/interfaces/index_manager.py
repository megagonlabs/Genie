"""Abstract contract for search index provisioning and verification."""

from abc import ABC, abstractmethod


class IIndexManager(ABC):
    """Defines lifecycle operations for the backing search index."""

    @abstractmethod
    def create_index(self) -> None:
        """Provision the index if it does not already exist.

        Implementations should create the required index structures in a
        controlled, explicit way and must not silently mutate incompatible
        existing indexes.
        """
        pass

    @abstractmethod
    def verify_index(self) -> None:
        """Validate that the configured index already exists and is usable.

        Implementations should raise a configuration-focused error when the
        runtime environment is missing the expected index or when the index
        schema is incompatible with the library configuration.
        """
        pass
