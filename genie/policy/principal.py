"""Principal identities used for policy decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    """The acting identity for a memory operation.

    Attributes:
        id: Stable principal id, for example `"user:alice"` or
            `"agent:planner"`.
        roles: Role names granted to this principal.
        metadata: Optional application metadata about the principal.
    """

    id: str = "system"
    roles: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: "Principal | str | None") -> "Principal":
        """Convert common principal representations into a `Principal`.

        Args:
            value: Existing principal, principal id string, or `None`.

        Returns:
            The provided principal, a new principal with the provided id, or the
            default system principal.
        """

        if isinstance(value, Principal):
            return value
        if isinstance(value, str):
            return cls(id=value)
        return cls()
