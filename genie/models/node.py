"""Pydantic data model representing a stored Genie node."""

from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List


class Node(BaseModel):
    """Represents a workspace or memory node in the Genie hierarchy.

    The model stores hierarchical information, arbitrary payload and metadata,
    and optional non-tree relationships. Direct child identifiers are derived
    from the persistence layer so callers can inspect the tree structure
    without relying on a duplicated relationships field.
    """

    node_id: str
    name: str
    parent_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    relationships: Dict[str, List[str]] = Field(default_factory=dict)
    child_ids: List[str] = Field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        """Return ``True`` when the node has no direct children."""
        return not bool(self.child_ids)
