from abc import ABC, abstractmethod
from typing import List


class IEmbedder(ABC):
    @abstractmethod
    def generate_vector(self, text: str) -> List[float]:
        pass
