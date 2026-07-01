"""Typed relationships between memory blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from genie.memory.block import utc_now


class LinkRelations:
    """Reserved Genie link relation constants.

    Attributes:
        MOUNT: Symbolic mount relation. Mount links point from a source block
            to another memory root. They do not imply parent ownership, subtree
            membership, recursive delete ownership, or access inheritance.
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

    Links represent non-hierarchical relationships. They do not imply parent
    ownership, subtree membership, or access inheritance.

    Attributes:
        source_id: Id of the block the link starts from.
        target_id: Id of the block the link points to.
        relation: Application-defined relationship type.
        id: Stable link identifier.
        metadata: Optional relationship metadata.
        created_at: UTC timestamp for link creation.
    """

    source_id: str
    target_id: str
    relation: str
    id: str = field(default_factory=new_link_id)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
