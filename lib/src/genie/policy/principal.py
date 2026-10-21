"""Principal identities for policy decisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    """The acting identity for a memory operation.

    Attributes:
        id: Stable principal id, for example `"user:alice"` or
            `"agent:planner"`. Defaults to `"system"`.
        roles: Role names that this principal has.
        metadata: Optional application metadata about the principal. The
            principal keeps a deep copy.

    Raises:
        TypeError: If `id` is not a string, `roles` is not a set of strings,
            or `metadata` is not a dictionary.
        ValueError: If `id` is empty or `roles` contains an empty string.
    """

    id: str = "system"
    roles: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Check the identity fields, and copy the containers that the caller owns."""

        if not isinstance(self.id, str):
            raise TypeError("Principal id must be a string.")
        if not self.id:
            raise ValueError("Principal id must not be empty.")
        object.__setattr__(self, "roles", _normalize_names(self.roles, field_name="roles"))
        if not isinstance(self.metadata, dict):
            raise TypeError("Principal metadata must be a dictionary.")
        object.__setattr__(self, "metadata", deepcopy(self.metadata))

    @classmethod
    def coerce(cls, value: Principal | str | None) -> Principal:
        """Change a common principal form into a `Principal`.

        Args:
            value: A `Principal`, a principal id string, or `None`.

        Returns:
            The same principal, a new principal with the given id, or the
            default `system` principal.

        Raises:
            TypeError: If `value` is not a principal, string, or `None`.
            ValueError: If `value` is an empty string.
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
    """Check principal role names, and return them as a `frozenset`."""

    if not isinstance(values, set | frozenset):
        raise TypeError(f"Principal {field_name} must be a set of strings.")
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"Principal {field_name} must contain only strings.")
    if any(not value for value in values):
        raise ValueError(f"Principal {field_name} must not contain empty strings.")
    return frozenset(values)
