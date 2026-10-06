"""Typed relationships between memory blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from genie.memory._validation import require_non_empty_str
from genie.memory.block import utc_now


class LinkRelations:
    """Reserved Genie link relation constants.

    Attributes:
        MOUNT: Symbolic mount relation. Mount links point from a source block
            to another memory root. They do not imply parent/child ownership,
            subtree membership, recursive delete ownership, or access
            inheritance.
    """

    MOUNT = "genie.mount"


def new_link_id() -> str:
    """Create a unique memory link id.

    Returns:
        A string id with the `link_` prefix.
    """

    return f"link_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class MemoryLink:
    """A typed edge between two memory blocks.

    Links represent relationships that are separate from parent/child
    ownership. They do not imply subtree membership or access inheritance.

    Attributes:
        source_id: Id of the block the link starts from.
        target_id: Id of the block the link points to.
        relation: Application-defined relationship type.
        id: Stable link identifier.
        metadata: Optional relationship metadata. `Memory.update_link` merges
            metadata while preserving the other link fields.
        created_at: UTC timestamp for link creation.

    Raises:
        TypeError: If `source_id`, `target_id`, `relation`, or `id` is not a
            string.
        ValueError: If `source_id`, `target_id`, `relation`, or `id` is empty.
    """

    source_id: str
    target_id: str
    relation: str
    id: str = field(default_factory=new_link_id)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Validate the endpoint, relation, and identity fields."""

        require_non_empty_str(self.source_id, field_name="MemoryLink source_id")
        require_non_empty_str(self.target_id, field_name="MemoryLink target_id")
        require_non_empty_str(self.relation, field_name="MemoryLink relation")
        require_non_empty_str(self.id, field_name="MemoryLink id")
