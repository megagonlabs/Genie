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
        scope: Search scope normalized to a `Scope` value.
        types: Optional block types normalized to an immutable tuple.
        filters: Metadata or block-field filters. Human-friendly operators are
            normalized to canonical `$...` operator names.
        limit: Maximum number of results. `0` is allowed; negative values are
            rejected.
    """

    text: str | None = None
    root_id: str | None = None
    scope: Scope = Scope.GLOBAL
    types: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 20

    def __init__(
        self,
        text: str | None = None,
        root_id: str | None = None,
        scope: Scope | str = Scope.GLOBAL,
        types: list[str] | tuple[str, ...] = (),
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> None:
        """Validate inputs and store their canonical forms.

        Args:
            text: Optional text query.
            root_id: Optional block id that anchors scoped searches.
            scope: Search scope as a `Scope` value or string.
            types: Optional block types supplied as a list or tuple.
            filters: Metadata or block-field filters.
            limit: Maximum number of results.

        Raises:
            TypeError: If text, root, types, filters, or limit have invalid
                runtime types.
            ValueError: If `scope` is invalid, `limit` is negative, or a
                filter uses an unknown operator.
        """

        if text is not None and not isinstance(text, str):
            raise TypeError("MemoryQuery text must be a string or None.")
        if root_id is not None and not isinstance(root_id, str):
            raise TypeError("MemoryQuery root_id must be a string or None.")
        normalized_scope = Scope(scope)
        if not isinstance(types, list | tuple):
            raise TypeError("MemoryQuery types must be a list or tuple of strings.")
        if any(not isinstance(type_name, str) for type_name in types):
            raise TypeError("MemoryQuery types must contain only strings.")
        normalized_types = tuple(types)
        normalized_filters = normalize_filters(filters)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("MemoryQuery limit must be an integer.")
        if limit < 0:
            raise ValueError("MemoryQuery limit must be greater than or equal to zero.")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "root_id", root_id)
        object.__setattr__(self, "scope", normalized_scope)
        object.__setattr__(self, "types", normalized_types)
        object.__setattr__(self, "filters", normalized_filters)
        object.__setattr__(self, "limit", limit)
