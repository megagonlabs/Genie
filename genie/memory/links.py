from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from genie.memory.block import utc_now


def new_link_id() -> str:
    return f"link_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class MemoryLink:
    """A typed edge between two memory blocks."""

    source_id: str
    target_id: str
    relation: str
    id: str = field(default_factory=new_link_id)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
