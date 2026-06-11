from abc import ABC, abstractmethod


class IIndexManager(ABC):
    @abstractmethod
    def initialize_index(self) -> None:
        pass
