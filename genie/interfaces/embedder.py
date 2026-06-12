"""Abstract embedder contract used by Genie search workflows."""

from abc import ABC, abstractmethod
from typing import List


class IEmbedder(ABC):
    """Defines the interface for text-to-vector embedding backends."""

    @abstractmethod
    def generate_vector(self, text: str) -> List[float]:
        """Generate a dense vector representation for a piece of text.

        Parameters:
        text:
            The text that should be embedded.

        Returns:
        list[float]
            The embedding vector produced by the backend.
        """
        pass
