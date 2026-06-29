from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    id: str = "system"
    roles: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: "Principal | str | None") -> "Principal":
        if isinstance(value, Principal):
            return value
        if isinstance(value, str):
            return cls(id=value)
        return cls()
