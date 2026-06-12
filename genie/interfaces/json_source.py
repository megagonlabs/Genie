"""Abstract contract for hierarchical node persistence backends."""

from abc import ABC, abstractmethod
from typing import List, Optional
from genie.models.node import Node


class IJsonSource(ABC):
    """Defines the interface for storing and retrieving Genie nodes."""

    @abstractmethod
    def upsert_node(self, node: Node, record_status: Optional[str] = None, last_error: Optional[str] = None) -> Node:
        """Create or update a node in the persistence layer.

        Parameters:
        node:
            The node to insert or update.
        record_status:
            Optional indexing lifecycle status to persist alongside the node.
        last_error:
            Optional diagnostic message associated with the current status.

        Returns:
        Node
            The persisted node.
        """
        pass

    @abstractmethod
    def get_node(self, node_id: str) -> Optional[Node]:
        """Fetch a node by its identifier.

        Parameters:
        node_id:
            The exact node identifier to load.

        Returns:
        Node | None
            The node when found, otherwise ``None``.
        """
        pass

    @abstractmethod
    def get_children(self, parent_id: str) -> List[str]:
        """Return the direct child node identifiers for a parent node.

        Parameters:
        parent_id:
            The parent node identifier.

        Returns:
        list[str]
            Sorted child node identifiers.
        """
        pass

    @abstractmethod
    def delete_node(self, node_id: str) -> bool:
        """Delete a node and any associated subtree data.

        Parameters:
        node_id:
            The node identifier to remove.

        Returns:
        bool
            ``True`` when the node existed and was deleted, otherwise ``False``.
        """
        pass

    @abstractmethod
    def get_absolute_path(self, node_id: str) -> str:
        """Resolve the absolute hierarchical path for a node.

        Parameters:
        node_id:
            The node identifier whose path should be resolved.

        Returns:
        str
            The absolute path rooted at ``/``.
        """
        pass

    @abstractmethod
    def set_record_status(self, node_id: str, record_status: str, last_error: Optional[str] = None) -> None:
        """Update the indexing lifecycle fields for a stored node.

        Parameters:
        node_id:
            The node whose lifecycle state should be updated.
        record_status:
            The new lifecycle state.
        last_error:
            Optional diagnostic message to persist with the state.
        """
        pass
