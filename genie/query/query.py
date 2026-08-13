"""Search query model used by memory APIs and backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from genie.query.filters import normalize_filters
from genie.query.scope import Scope


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Search parameters normalized before reaching a backend.

    Attributes:
        text: Optional text query.
        root_id: Optional block id that anchors scoped searches.
        scope: Search scope as a `Scope` value or string.
        types: Optional block types to include.
        filters: Metadata or block-field filters. Human-friendly operators are
            normalized to canonical `$...` operator names.
        limit: Maximum number of results. `0` is allowed; negative values are
            rejected.
    """

    text: str | None = None
    root_id: str | None = None
    scope: Scope | str = Scope.GLOBAL
    types: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 20

    def __post_init__(self) -> None:
        """Normalize scope, type, and filter inputs after initialization.

        Raises:
            TypeError: If text, root, types, filters, or limit have invalid
                runtime types.
            ValueError: If `scope` is invalid or `limit` is negative.
        """

        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("MemoryQuery text must be a string or None.")
        if self.root_id is not None and not isinstance(self.root_id, str):
            raise TypeError("MemoryQuery root_id must be a string or None.")
        object.__setattr__(self, "scope", Scope(self.scope))
        if not isinstance(self.types, list | tuple):
            raise TypeError("MemoryQuery types must be a list or tuple of strings.")
        if any(not isinstance(type_name, str) for type_name in self.types):
            raise TypeError("MemoryQuery types must contain only strings.")
        object.__setattr__(self, "types", tuple(self.types))
        object.__setattr__(self, "filters", normalize_filters(self.filters))
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("MemoryQuery limit must be an integer.")
        if self.limit < 0:
            raise ValueError("MemoryQuery limit must be greater than or equal to zero.")
