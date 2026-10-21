"""Search result model."""

from __future__ import annotations

import math
from dataclasses import dataclass

from genie.memory.block import MemoryBlock


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A block that a search operation returns.

    Attributes:
        block: Matching memory block.
        score: Ranking score that the backend defines. `SearchResult` stores it
            as a `float`. The in-memory backend puts higher scores first.
        reason: Optional human-readable reason for the match.

    Raises:
        TypeError: If a field has a runtime type that is not valid, or `score`
            is a `bool`.
        ValueError: If `score` is not finite.
    """

    block: MemoryBlock
    score: float = 1.0
    reason: str | None = None

    def __post_init__(self) -> None:
        """Check the portable result shape that crosses backend boundaries, and store `score` as a `float`."""

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
