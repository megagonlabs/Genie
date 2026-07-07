"""Declarative access rules for memory blocks."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from genie.policy.principal import Principal

BUILTIN_OPERATION_NAMES = frozenset(
    {
        "read",
        "write",
        "search",
        "traverse_up",
        "traverse_down",
    }
)
"""Operation names reserved by Genie's core memory API."""


@dataclass(frozen=True, slots=True)
class AccessRule:
    """A rule that grants or denies one operation.

    Args:
        allow_all: Whether every principal is allowed.
        principals: Explicit principal ids allowed when `allow_all` is false.
        roles: Principal roles allowed when `allow_all` is false.
    """

    allow_all: bool = True
    principals: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def allow(cls) -> "AccessRule":
        """Create a rule that allows every principal.

        Returns:
            An allow-all access rule.
        """

        return cls(allow_all=True)

    @classmethod
    def deny(cls) -> "AccessRule":
        """Create a rule that denies every principal.

        Returns:
            A deny-all access rule.
        """

        return cls(allow_all=False)

    @classmethod
    def only(
        cls,
        *,
        principals: set[str] | frozenset[str] | None = None,
        roles: set[str] | frozenset[str] | None = None,
    ) -> "AccessRule":
        """Create a rule that allows only listed principals or roles.

        Args:
            principals: Principal ids to allow.
            roles: Role names to allow.

        Returns:
            A selective access rule.
        """

        return cls(
            allow_all=False,
            principals=frozenset(principals or ()),
            roles=frozenset(roles or ()),
        )

    def allows(self, principal: Principal) -> bool:
        """Return whether the principal satisfies this rule.

        Args:
            principal: Principal attempting an operation.

        Returns:
            `True` when the principal is allowed.
        """

        if self.allow_all:
            return True
        if principal.id in self.principals:
            return True
        return bool(self.roles.intersection(principal.roles))


class _FrozenOperationRules(Mapping[str, AccessRule]):
    """Immutable, deepcopy-friendly mapping for custom operation rules."""

    def __init__(self, rules: Mapping[str, AccessRule]) -> None:
        """Create an immutable copy of operation rules."""

        self._rules = dict(rules)

    def __getitem__(self, key: str) -> AccessRule:
        """Return the rule for an operation name."""

        return self._rules[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over operation names."""

        return iter(self._rules)

    def __len__(self) -> int:
        """Return the number of operation rules."""

        return len(self._rules)

    def __deepcopy__(self, memo: dict[int, object]) -> "_FrozenOperationRules":
        """Return a copied immutable mapping."""

        copied = self.__class__(deepcopy(self._rules, memo))
        memo[id(self)] = copied
        return copied

    def __repr__(self) -> str:
        """Return a developer-friendly representation."""

        return repr(self._rules)


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Access policy attached to a memory block.

    Each field controls one operation family. Scope limits are enforced
    separately by `MemoryHandle`. Applications can attach named custom
    operations through `operations` and evaluate them with `PolicyEngine`.

    Attributes:
        read: Permission to read the block value and metadata.
        write: Permission to update or delete the block.
        search: Permission for the block to appear in search results.
        traverse_up: Permission to traverse from this block to ancestors.
        traverse_down: Permission to traverse from this block to descendants.
        operations: Optional application-defined operation rules, such as
            `"export"` or `"approve"`. These names cannot override Genie's
            built-in operation fields.
    """

    read: AccessRule = field(default_factory=AccessRule.allow)
    write: AccessRule = field(default_factory=AccessRule.allow)
    search: AccessRule = field(default_factory=AccessRule.allow)
    traverse_up: AccessRule = field(default_factory=AccessRule.allow)
    traverse_down: AccessRule = field(default_factory=AccessRule.allow)
    operations: Mapping[str, AccessRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and freeze custom operation rules."""

        reserved = BUILTIN_OPERATION_NAMES.intersection(self.operations)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"Custom policy operations cannot override built-ins: {names}.")
        object.__setattr__(self, "operations", _FrozenOperationRules(self.operations))

    @classmethod
    def public(cls) -> "MemoryPolicy":
        """Create the default public policy.

        Returns:
            A policy that allows every operation to every principal.
        """

        return cls()
