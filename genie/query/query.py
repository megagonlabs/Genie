from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from genie.query.filters import normalize_filters
from genie.query.scope import Scope


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str | None = None
    root_id: str | None = None
    scope: Scope | str = Scope.GLOBAL
    types: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", Scope(self.scope))
        if isinstance(self.types, list):
            object.__setattr__(self, "types", tuple(self.types))
        object.__setattr__(self, "filters", normalize_filters(self.filters))
        if self.limit < 0:
            raise ValueError("MemoryQuery limit must be greater than or equal to zero.")
