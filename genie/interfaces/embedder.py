from abc import ABC, abstractmethod
from typing import List


class IEmbedder(ABC):
    """Contract for text embedding models."""

    @abstractmethod
    def generate_vector(self, text: str) -> List[float]:
        """
        Translates raw text into a searchable dense vector.
        Must return a flat list of floats.
        """
        pass
