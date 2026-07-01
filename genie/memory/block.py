"""Memory block primitives.

This module contains the core addressable memory unit used throughout Genie.
`MemoryBlock` is intentionally storage-neutral and content-agnostic: adapters
may persist it in SQL, documents, graph nodes, object stores, or another
representation, and applications decide what each block type and value mean.
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
    """The smallest independently addressable unit of Genie memory.

    A block represents the unit at which identity, parent placement, metadata,
    provenance, access policy, and lifecycle are tracked. Genie enforces the
    structural block and parent/child containment model; applications define
    the domain meaning of block types and values.

    Attributes:
        type: Application-defined category such as `"note"` or `"task"`.
        value: Encoded memory payload. Public `Memory` methods return decoded
            values; backends store encoded values.
        id: Stable block identifier.
        parent_id: Optional parent block id for containment placement.
        content_type: Codec content type used to decode `value`.
        metadata: Arbitrary JSON-compatible metadata used for filtering and
            adapter-specific indexing.
        policy: Optional access policy attached directly to this block.
        created_at: UTC timestamp for initial creation.
        updated_at: UTC timestamp for the latest update.
        created_by: Principal id that created the block, when known.
        version: Monotonically increasing version managed by `Memory`.
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

    def with_updates(self, **changes: Any) -> "MemoryBlock":
        """Return a copy of the block with selected fields replaced.

        Args:
            **changes: Dataclass fields to replace.

        Returns:
            A new `MemoryBlock` instance.
        """

        if "metadata" in changes and changes["metadata"] is None:
            changes["metadata"] = {}
        return replace(self, **changes)
