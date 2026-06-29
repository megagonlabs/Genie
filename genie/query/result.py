"""Search result model."""

from __future__ import annotations

from dataclasses import dataclass

from genie.memory.block import MemoryBlock


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A block returned from a search operation.

    Attributes:
        block: Matching memory block.
        score: Backend-defined ranking score. Higher scores sort first in the
            in-memory backend.
        reason: Optional human-readable reason for the match.
    """

    block: MemoryBlock
    score: float = 1.0
    reason: str | None = None
