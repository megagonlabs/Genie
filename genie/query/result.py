from __future__ import annotations

from dataclasses import dataclass

from genie.memory.block import MemoryBlock


@dataclass(frozen=True, slots=True)
class SearchResult:
    block: MemoryBlock
    score: float = 1.0
    reason: str | None = None
