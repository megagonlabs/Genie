from abc import ABC, abstractmethod
from typing import List, Optional
from genie.models.node import Node


class IJsonSource(ABC):
    @abstractmethod
    def upsert_node(self, node: Node) -> Node:
        pass

    @abstractmethod
    def get_node(self, node_id: str) -> Optional[Node]:
        pass

    @abstractmethod
    def get_children(self, parent_id: str) -> List[str]:
        pass

    @abstractmethod
    def delete_node(self, node_id: str) -> bool:
        pass

    @abstractmethod
    def get_absolute_path(self, node_id: str) -> str:
        pass
