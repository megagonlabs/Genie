from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List


class Node(BaseModel):
    node_id: str
    name: str
    parent_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    relationships: Dict[str, List[str]] = Field(default_factory=dict)
    child_ids: List[str] = Field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return not bool(self.child_ids)
