from abc import ABC, abstractmethod
from typing import List, Optional, Any
from models.memory import MemoryItem, MemoryScope


class BaseMemoryStorage(ABC):
    @abstractmethod
    def save(self, memory: MemoryItem) -> None:
        pass

    @abstractmethod
    def get(self, memory_id: str) -> Optional[MemoryItem]:
        pass

    @abstractmethod
    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:  # <- NEW
        pass

    @abstractmethod
    def update(self, memory_id: str, new_content: Any) -> None:
        pass

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        pass
