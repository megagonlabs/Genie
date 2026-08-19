"""Principal identities used for policy decisions."""

from __future__ import annotations

from copy import deepcopy
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

    def __post_init__(self) -> None:
        """Validate identity fields and isolate caller-owned containers."""

        if not isinstance(self.id, str):
            raise TypeError("Principal id must be a string.")
        if not self.id:
            raise ValueError("Principal id must not be empty.")
        object.__setattr__(self, "roles", _normalize_names(self.roles, field_name="roles"))
        if not isinstance(self.metadata, dict):
            raise TypeError("Principal metadata must be a dictionary.")
        object.__setattr__(self, "metadata", deepcopy(self.metadata))

    @classmethod
    def coerce(cls, value: "Principal | str | None") -> "Principal":
        """Convert common principal representations into a `Principal`.

        Args:
            value: Existing principal, principal id string, or `None`.

        Returns:
            The provided principal, a new principal with the provided id, or the
            default system principal.

        Raises:
            TypeError: If `value` is not a principal, string, or `None`.
        """

        if isinstance(value, Principal):
            return value
        if isinstance(value, str):
            return cls(id=value)
        if value is None:
            return cls()
        raise TypeError("principal must be a Principal, string id, or None.")


def _normalize_names(
    values: set[str] | frozenset[str],
    *,
    field_name: str,
) -> frozenset[str]:
    """Return validated principal role names as an immutable set."""

    if not isinstance(values, set | frozenset):
        raise TypeError(f"Principal {field_name} must be a set of strings.")
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"Principal {field_name} must contain only strings.")
    if any(not value for value in values):
        raise ValueError(f"Principal {field_name} must not contain empty strings.")
    return frozenset(values)
