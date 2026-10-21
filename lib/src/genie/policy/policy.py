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
"""Operation names that the core memory API of Genie reserves."""


@dataclass(frozen=True, slots=True)
class AccessRule:
    """A rule that grants or denies one operation.

    - **Fail closed:** the defaults deny everyone. `AccessRule()` is the same as
      `AccessRule.deny()`.
    - **Grants:** a rule made from `principals`, `roles`, or `allow_owner`
      allows only those.

    Attributes:
        allow_all: If `True`, all principals pass.
        principals: Explicit principal ids that pass when `allow_all` is false.
        roles: Principal roles that pass when `allow_all` is false.
        allow_owner: Whether the block creator passes when `allow_all` is
            false. The owner is the principal id in `MemoryBlock.created_by`.

    Raises:
        TypeError: If a flag is not a boolean, or `principals` or `roles` is
            not a set of strings.
        ValueError: If `principals` or `roles` contains an empty string, or
            `allow_all` is `True` together with `principals`, `roles`, or
            `allow_owner`.
    """

    allow_all: bool = False
    principals: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    allow_owner: bool = False

    def __post_init__(self) -> None:
        """Check the flags, and freeze the principal and role sets."""

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
        if self.allow_all and (self.principals or self.roles or self.allow_owner):
            raise ValueError(
                "AccessRule allow_all cannot be combined with principals, roles, or allow_owner; "
                "use AccessRule.allow() or AccessRule.only(...)."
            )

    @classmethod
    def allow(cls) -> AccessRule:
        """Create a rule that allows all principals.

        Returns:
            An allow-all access rule.
        """

        return cls(allow_all=True)

    @classmethod
    def deny(cls) -> AccessRule:
        """Create a rule that denies all principals.

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
    ) -> AccessRule:
        """Create a rule that allows only listed principals or roles.

        Args:
            principals: Principal ids to allow.
            roles: Role names to allow.
            owner: If `True`, the block creator also passes.

        Returns:
            A rule that allows only the listed principals and roles, and the
            owner if `owner` is `True`.
        """

        return cls(
            allow_all=False,
            principals=frozenset(principals or ()),
            roles=frozenset(roles or ()),
            allow_owner=owner,
        )

    @classmethod
    def owner(cls) -> AccessRule:
        """Create a rule that allows only the block creator.

        Returns:
            An owner-only access rule.
        """

        return cls(allow_all=False, allow_owner=True)

    def allows(self, principal: Principal, *, owner_id: str | None = None) -> bool:
        """Return whether the principal passes this rule.

        Args:
            principal: Principal that tries an operation.
            owner_id: Principal id of the block creator. With `None`, the owner
                grant does not apply.

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
    """Mapping of custom operation rules that cannot change and that `deepcopy` can copy."""

    def __init__(self, rules: Mapping[str, AccessRule]) -> None:
        """Create an immutable copy of operation rules.

        Args:
            rules: Operation rules to copy.
        """

        self._rules = dict(rules)

    def __getitem__(self, key: str) -> AccessRule:
        """Return the rule for an operation name."""

        return self._rules[key]

    def __iter__(self) -> Iterator[str]:
        """Return an iterator over the operation names."""

        return iter(self._rules)

    def __len__(self) -> int:
        """Return the number of operation rules."""

        return len(self._rules)

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenOperationRules:
        """Return a copy of the mapping that also cannot change."""

        copied = self.__class__(deepcopy(self._rules, memo))
        memo[id(self)] = copied
        return copied

    def __repr__(self) -> str:
        """Return a representation for developers."""

        return repr(self._rules)


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Access policy attached to a memory block.

    - **One field for each family:** each built-in field controls one operation
      family.
    - **Scope is separate:** `MemoryHandle` applies the scope limits, not the
      policy.
    - **Custom operations:** applications can attach named custom operations
      through `operations`. `PolicyEngine` evaluates them.
    - **Owner-only default:** each built-in field defaults to
      `AccessRule.owner()`. So `MemoryPolicy()` is the same as
      `MemoryPolicy.owner_private()`. A policy that sets only some fields keeps
      the other operations owner-only.

    Attributes:
        read: Permission to read the block value and metadata.
        write: Permission to update or delete the block. It also controls new
            children under the block, new links from it, and membership
            changes when the block is a structure container.
        search: Permission for the block to appear in search results.
        traverse_up: Permission to go from this block to its ancestors.
        traverse_down: Permission to go from this block to its descendants. A
            new child under the block also needs it.
        operations: Optional application-defined operation rules, such as
            `"export"` or `"approve"`. These names cannot replace the built-in
            operation fields of Genie.

    Raises:
        TypeError: If a built-in field is not an `AccessRule`, or `operations`
            is not a mapping of string names to `AccessRule` values.
        ValueError: If `operations` uses a name that Genie reserves, or an
            empty name.
    """

    read: AccessRule = field(default_factory=AccessRule.owner)
    write: AccessRule = field(default_factory=AccessRule.owner)
    search: AccessRule = field(default_factory=AccessRule.owner)
    traverse_up: AccessRule = field(default_factory=AccessRule.owner)
    traverse_down: AccessRule = field(default_factory=AccessRule.owner)
    operations: Mapping[str, AccessRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Check the built-in fields, and freeze the custom operation rules."""

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
    def public(cls) -> MemoryPolicy:
        """Create a public policy.

        Returns:
            A policy that allows all built-in operations to all principals.
        """

        allow_rule = AccessRule.allow()
        return cls(
            read=allow_rule,
            write=allow_rule,
            search=allow_rule,
            traverse_up=allow_rule,
            traverse_down=allow_rule,
        )

    @classmethod
    def owner_private(cls) -> MemoryPolicy:
        """Create a policy that allows only the block creator.

        - **Owner:** a policy check gets the owner from `MemoryBlock.created_by`.
        - **Default:** this is the default policy for new `Memory` blocks.

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
    ) -> MemoryPolicy:
        """Create a policy that allows the creator plus listed principals or roles.

        It suits managed memory systems in which the creator keeps access, and
        a supervisor, a service principal, or an admin role also gets read,
        write, search, and traversal access.

        Args:
            principals: Additional principal ids to allow.
            roles: Additional role names to allow.

        Returns:
            A policy that allows the creator, the listed principals, and the
            listed roles for the built-in memory operations.
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
    """Check access-rule names, and return them as a `frozenset`."""

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
    """Check the application-defined operation rules, and return a frozen copy."""

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
