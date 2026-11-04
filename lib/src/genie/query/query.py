"""Search query model for the memory APIs and the backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from genie.memory._validation import require_non_empty_str
from genie.query.filters import normalize_filters
from genie.query.result import SearchCursor
from genie.query.scope import Scope


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Search parameters in normalized form, before they get to a backend.

    Attributes:
        text: Optional text query.
        root_id: Optional block id that is the anchor of a scoped search.
        scope: Search scope as a `Scope` value, or `None` for the default
            scope of the caller. `Memory` uses `global`, and a `MemoryHandle`
            uses its `default_scope`. `Memory` gives hooks and backends a `Scope`.
        types: Optional block types, as a tuple.
        filters: Metadata or block-field filters. Friendly operators change to
            canonical `$...` operator names.
        limit: Maximum number of results. A backend returns at most `limit`
            results. `Memory.search` counts only the results that the caller
            can see. The value `0` is allowed, and a negative value raises
            `ValueError`.
        after: Optional `SearchCursor`. The search returns only the results
            that come after this position in the search order.
    """

    text: str | None = None
    root_id: str | None = None
    scope: Scope | None = None
    types: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 20
    after: SearchCursor | None = None

    def __init__(
        self,
        text: str | None = None,
        root_id: str | None = None,
        scope: Scope | str | None = None,
        types: list[str] | tuple[str, ...] = (),
        filters: dict[str, Any] | None = None,
        limit: int = 20,
        after: SearchCursor | None = None,
    ) -> None:
        """Check the inputs, and store them in canonical form.

        Args:
            text: Optional text query.
            root_id: Optional block id that is the anchor of a scoped search.
            scope: Search scope as a `Scope` value or a string, or `None`
                for the default scope of the caller.
            types: Optional block types, as a list or a tuple.
            filters: Metadata or block-field filters.
            limit: Maximum number of results.
            after: Optional position. The search returns only the results
                after it.

        Raises:
            TypeError: If text, root, types, filters, limit, or after have
                runtime types that are not valid.
            ValueError: If `root_id` or an entry of `types` is empty or
                contains only whitespace, `scope` is not a known scope,
                `limit` is negative, or a filter uses an unknown operator.
        """

        if text is not None and not isinstance(text, str):
            raise TypeError("MemoryQuery text must be a string or None.")
        if root_id is not None and not isinstance(root_id, str):
            raise TypeError("MemoryQuery root_id must be a string or None.")
        if root_id is not None:
            require_non_empty_str(root_id, field_name="MemoryQuery root_id")
        normalized_scope = None if scope is None else Scope(scope)
        if not isinstance(types, list | tuple):
            raise TypeError("MemoryQuery types must be a list or tuple of strings.")
        if any(not isinstance(type_name, str) for type_name in types):
            raise TypeError("MemoryQuery types must contain only strings.")
        for type_name in types:
            require_non_empty_str(type_name, field_name="MemoryQuery types entry")
        normalized_types = tuple(types)
        normalized_filters = normalize_filters(filters)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("MemoryQuery limit must be an integer.")
        if limit < 0:
            raise ValueError("MemoryQuery limit must be greater than or equal to zero.")
        if after is not None and not isinstance(after, SearchCursor):
            raise TypeError("MemoryQuery after must be a SearchCursor or None.")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "root_id", root_id)
        object.__setattr__(self, "scope", normalized_scope)
        object.__setattr__(self, "types", normalized_types)
        object.__setattr__(self, "filters", normalized_filters)
        object.__setattr__(self, "limit", limit)
        object.__setattr__(self, "after", after)
