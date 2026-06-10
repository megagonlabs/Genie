from abc import ABC, abstractmethod
from typing import List, Optional

# Production absolute imports
from genie.models.node import Node


class IJsonSource(ABC):
    """Structural & Payload Storage Contract"""

    @abstractmethod
    def upsert_node(self, node: Node) -> Node:
        """Creates or updates a node. Updates parent's edges if parent_id exists."""
        pass

    @abstractmethod
    def get_node(self, node_id: str) -> Optional[Node]:
        """Returns the full Node object."""
        pass

    @abstractmethod
    def get_children(self, parent_id: str) -> List[str]:
        """Returns a list of child node_ids."""
        pass

    @abstractmethod
    def delete_node(self, node_id: str) -> bool:
        """Deletes the node and recursively deletes descendants."""
        pass

    @abstractmethod
    def get_absolute_path(self, node_id: str) -> str:
        """Recursively builds the absolute path string (e.g., /node_a/node_b)."""
        pass
