"""Memory block model.

- **Memory unit:** this module holds the memory unit that all of Genie uses.
- **Backend-neutral:** adapters can store `MemoryBlock` in SQL, documents,
  graph nodes, object stores, or another form.
- **Meaning:** applications decide what each block type and value mean.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from genie.memory._validation import require_non_empty_str
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
    """The smallest memory unit that Genie stores and gets back.

    - **What Genie tracks:** a block is the unit for identity, parent placement,
      metadata, source information, access policy, and the version number.
    - **What Genie enforces:** the block shape and the parent and child
      ownership model. Applications decide what block types and values mean.

    Attributes:
        type: Application-defined category, such as `"note"` or `"task"`.
        value: Encoded memory payload. Public `Memory` methods return decoded
            values, and backends store encoded values.
        id: Stable block id.
        parent_id: Optional parent block id, for ownership placement.
        content_type: Codec content type that decodes `value`.
        metadata: Any JSON-compatible metadata, for filters and
            adapter-specific indexes.
        policy: Optional access policy attached directly to this block. With
            `None`, the policy engine uses `MemoryPolicy.owner_private()`.
        created_at: UTC timestamp of the first creation.
        updated_at: UTC timestamp of the latest update.
        created_by: Principal id that created the block, if known.
        version: Version number. It starts at 1, and `Memory` increases it on
            each update.

    Raises:
        TypeError: If `id`, `type`, or `parent_id` is not a string. A
            `parent_id` of `None` is allowed.
        ValueError: If `id`, `type`, or `parent_id` is empty or contains only
            whitespace.
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

    def __post_init__(self) -> None:
        """Check the id, type, and parent fields."""

        require_non_empty_str(self.id, field_name="MemoryBlock id")
        require_non_empty_str(self.type, field_name="MemoryBlock type")
        if self.parent_id is not None:
            require_non_empty_str(self.parent_id, field_name="MemoryBlock parent_id")

    def with_updates(self, **changes: Any) -> MemoryBlock:
        """Return a copy of the block with selected fields replaced.

        A `metadata` of `None` becomes an empty dictionary.

        Args:
            **changes: Dataclass fields to replace.

        Returns:
            A new `MemoryBlock` instance.
        """

        if "metadata" in changes and changes["metadata"] is None:
            changes["metadata"] = {}
        return replace(self, **changes)
