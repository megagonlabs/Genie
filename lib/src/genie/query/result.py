"""Search result and search cursor models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from genie.memory._validation import require_non_empty_str
from genie.memory.block import MemoryBlock


def _finite_score(score: object, *, owner: str) -> float:
    """Return a score as a finite `float`, or raise an error."""

    if isinstance(score, bool) or not isinstance(score, int | float):
        raise TypeError(f"{owner} score must be a number.")
    normalized_score = float(score)
    if not math.isfinite(normalized_score):
        raise ValueError(f"{owner} score must be finite.")
    return normalized_score


@dataclass(frozen=True, slots=True)
class SearchCursor:
    """A position in the search order, directly after one result.

    - **Search order:** results sort by `score` from high to low, then by
      `created_at` from old to new, then by block id in ascending order.
    - **Use:** give a cursor as `MemoryQuery.after`. The search then returns
      only the results that come after this position.
    - **No access check:** a cursor is only a position. It can name a block
      that the caller cannot see, and it shows nothing about that block.

    Attributes:
        score: Score of the result at this position. `SearchCursor` stores it
            as a `float`.
        created_at: `created_at` of the block at this position.
        block_id: Block id of the result at this position.

    Raises:
        TypeError: If `score` is not a number or is a `bool`, `created_at` is
            not a `datetime`, or `block_id` is not a string.
        ValueError: If `score` is not finite, `created_at` has no timezone,
            or `block_id` is empty.
    """

    score: float
    created_at: datetime
    block_id: str

    def __post_init__(self) -> None:
        """Check the fields, and store `score` as a `float`."""

        object.__setattr__(self, "score", _finite_score(self.score, owner="SearchCursor"))
        if not isinstance(self.created_at, datetime):
            raise TypeError("SearchCursor created_at must be a datetime.")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("SearchCursor created_at must be timezone-aware.")
        require_non_empty_str(self.block_id, field_name="SearchCursor block_id")


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A block that a search operation returns.

    Attributes:
        block: Matching memory block.
        score: Ranking score that the backend defines. `SearchResult` stores it
            as a `float`. Higher scores come first.
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
        normalized_score = _finite_score(self.score, owner="SearchResult")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("SearchResult reason must be a string or None.")
        object.__setattr__(self, "score", normalized_score)

    @property
    def cursor(self) -> SearchCursor:
        """Return the position directly after this result, for `MemoryQuery.after`."""

        return SearchCursor(score=self.score, created_at=self.block.created_at, block_id=self.block.id)
