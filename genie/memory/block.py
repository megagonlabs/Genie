from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from genie.policy.policy import MemoryPolicy


def new_memory_id() -> str:
    return f"mem_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MemoryBlock:
    """The smallest independently addressable unit of Genie memory."""

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
        if "metadata" in changes and changes["metadata"] is None:
            changes["metadata"] = {}
        return replace(self, **changes)
