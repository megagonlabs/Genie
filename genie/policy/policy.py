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
        allow_owner: Whether the block creator is allowed when `allow_all` is
            false. The owner is the principal id stored in
            `MemoryBlock.created_by`.
    """

    allow_all: bool = True
    principals: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    allow_owner: bool = False

    def __post_init__(self) -> None:
        """Validate flags and freeze principal and role collections."""

        if not isinstance(self.allow_all, bool):
            raise TypeError("AccessRule allow_all must be a boolean.")
        if not isinstance(self.allow_owner, bool):
            raise TypeError("AccessRule allow_owner must be a boolean.")
        object.__setattr__(
            self,
            "principals",
            _normalize_names(self.principals, field_name="principals"),
        )
        object.__setattr__(
            self,
            "roles",
            _normalize_names(self.roles, field_name="roles"),
        )

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
        owner: bool = False,
    ) -> "AccessRule":
        """Create a rule that allows only listed principals or roles.

        Args:
            principals: Principal ids to allow.
            roles: Role names to allow.
            owner: Whether to allow the block creator.

        Returns:
            A selective access rule.
        """

        return cls(
            allow_all=False,
            principals=frozenset(principals or ()),
            roles=frozenset(roles or ()),
            allow_owner=owner,
        )

    @classmethod
    def owner(cls) -> "AccessRule":
        """Create a rule that allows only the block creator.

        Returns:
            An owner-only access rule.
        """

        return cls(allow_all=False, allow_owner=True)

    def allows(self, principal: Principal, *, owner_id: str | None = None) -> bool:
        """Return whether the principal satisfies this rule.

        Args:
            principal: Principal attempting an operation.
            owner_id: Optional creator principal id for owner-aware rules.

        Returns:
            `True` when the principal is allowed.
        """

        if self.allow_all:
            return True
        if self.allow_owner and owner_id is not None and principal.id == owner_id:
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

        for field_name in (
            "read",
            "write",
            "search",
            "traverse_up",
            "traverse_down",
        ):
            if not isinstance(getattr(self, field_name), AccessRule):
                raise TypeError(f"MemoryPolicy {field_name} must be an AccessRule.")
        object.__setattr__(self, "operations", _freeze_operation_rules(self.operations))

    @classmethod
    def public(cls) -> "MemoryPolicy":
        """Create a public policy.

        Returns:
            A policy that allows every operation to every principal.
        """

        return cls()

    @classmethod
    def owner_private(cls) -> "MemoryPolicy":
        """Create a policy that allows only the block creator.

        The owner is resolved from `MemoryBlock.created_by` when a policy check
        runs. This is the default policy for new `Memory` blocks.

        Returns:
            A policy that allows only the creator to read, write, search, and
            traverse the block.
        """

        owner_rule = AccessRule.owner()
        return cls(
            read=owner_rule,
            write=owner_rule,
            search=owner_rule,
            traverse_up=owner_rule,
            traverse_down=owner_rule,
        )

    @classmethod
    def owner_or(
        cls,
        *,
        principals: set[str] | frozenset[str] | None = None,
        roles: set[str] | frozenset[str] | None = None,
    ) -> "MemoryPolicy":
        """Create a policy that allows the creator plus listed principals or roles.

        This is useful for managed memory systems where the creator should keep
        access, but a supervisor, service principal, or admin role also needs
        read, write, search, and traversal access.

        Args:
            principals: Additional principal ids to allow.
            roles: Additional role names to allow.

        Returns:
            A policy that allows the creator, listed principals, and listed
            roles for core memory operations.
        """

        rule = AccessRule.only(principals=principals, roles=roles, owner=True)
        return cls(
            read=rule,
            write=rule,
            search=rule,
            traverse_up=rule,
            traverse_down=rule,
        )


def _normalize_names(
    values: set[str] | frozenset[str],
    *,
    field_name: str,
) -> frozenset[str]:
    """Return validated access-rule names as an immutable set."""

    if not isinstance(values, set | frozenset):
        raise TypeError(f"AccessRule {field_name} must be a set of strings.")
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"AccessRule {field_name} must contain only strings.")
    if any(not value for value in values):
        raise ValueError(f"AccessRule {field_name} must not contain empty strings.")
    return frozenset(values)


def _freeze_operation_rules(
    rules: Mapping[str, AccessRule],
) -> _FrozenOperationRules:
    """Validate and freeze application-defined policy operation rules."""

    if not isinstance(rules, Mapping):
        raise TypeError("Custom policy operations must be a mapping.")
    validated: dict[str, AccessRule] = {}
    for name, rule in rules.items():
        if not isinstance(name, str):
            raise TypeError("Custom policy operation names must be strings.")
        if not name:
            raise ValueError("Custom policy operation names must not be empty.")
        if not isinstance(rule, AccessRule):
            raise TypeError(
                f"Custom policy operation {name!r} must use an AccessRule."
            )
        validated[name] = rule
    reserved = BUILTIN_OPERATION_NAMES.intersection(validated)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise ValueError(f"Custom policy operations cannot override built-ins: {names}.")
    return _FrozenOperationRules(validated)
