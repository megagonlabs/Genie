from abc import ABC, abstractmethod


class IIndexManager(ABC):
    """Contract for database schema and index lifecycle management."""

    @abstractmethod
    def initialize_index(self) -> None:
        """Safely creates or updates the required database indexes/schemas."""
        pass
