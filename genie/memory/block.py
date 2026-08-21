"""Memory block model.

This module contains the memory unit used throughout Genie. `MemoryBlock` is
backend-neutral: adapters may persist it in SQL, documents, graph nodes, object
stores, or another representation. Applications decide what each block type and
value mean.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from genie.policy.policy import MemoryPolicy


def new_memory_id() -> str:
    """Create a unique memory block id.

    Returns:
        A string id with the `mem_` prefix.
    """

    return f"mem_{uuid4().hex}"


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp.

    Returns:
        A `datetime` instance using the UTC timezone.
    """

    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MemoryBlock:
    """The smallest memory unit Genie stores and retrieves.

    A block is the unit where Genie tracks identity, parent placement,
    metadata, source information, access policy, and version history. Genie
    enforces the block shape and parent/child ownership model; applications
    decide what block types and values mean.

    Attributes:
        type: Application-defined category such as `"note"` or `"task"`.
        value: Encoded memory payload. Public `Memory` methods return decoded
            values; backends store encoded values.
        id: Stable block identifier.
        parent_id: Optional parent block id for ownership placement.
        content_type: Codec content type used to decode `value`.
        metadata: Arbitrary JSON-compatible metadata used for filtering and
            adapter-specific indexing.
        policy: Optional access policy attached directly to this block.
        created_at: UTC timestamp for initial creation.
        updated_at: UTC timestamp for the latest update.
        created_by: Principal id that created the block, when known.
        version: Increasing version number managed by `Memory`.
    """

    type: str
    value: Any
    id: str = field(default_factory=new_memory_id)
    parent_id: str | None = None
    content_type: str = "application/json"
    metadata: dict[str, Any] = field(default_factory=dict)
    policy: MemoryPolicy | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by: str | None = None
    version: int = 1

    def with_updates(self, **changes: Any) -> MemoryBlock:
        """Return a copy of the block with selected fields replaced.

        Args:
            **changes: Dataclass fields to replace.

        Returns:
            A new `MemoryBlock` instance.
        """

        if "metadata" in changes and changes["metadata"] is None:
            changes["metadata"] = {}
        return replace(self, **changes)
