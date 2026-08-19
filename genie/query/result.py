"""Search result model."""

from __future__ import annotations

import math
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

    Raises:
        TypeError: If a field has an invalid runtime type.
        ValueError: If `score` is not finite.
    """

    block: MemoryBlock
    score: float = 1.0
    reason: str | None = None

    def __post_init__(self) -> None:
        """Validate the portable result shape used across backend boundaries."""

        if not isinstance(self.block, MemoryBlock):
            raise TypeError("SearchResult block must be a MemoryBlock.")
        if isinstance(self.score, bool) or not isinstance(self.score, int | float):
            raise TypeError("SearchResult score must be a number.")
        normalized_score = float(self.score)
        if not math.isfinite(normalized_score):
            raise ValueError("SearchResult score must be finite.")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("SearchResult reason must be a string or None.")
        object.__setattr__(self, "score", normalized_score)
