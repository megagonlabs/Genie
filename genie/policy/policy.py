"""Declarative access rules for memory blocks."""

from __future__ import annotations

from dataclasses import dataclass, field

from genie.policy.principal import Principal


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


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Access policy attached to a memory block.

    Each field controls one operation family. Scope boundaries are enforced
    separately by `MemoryScope`.

    Attributes:
        read: Permission to read the block value and metadata.
        write: Permission to update or delete the block.
        search: Permission for the block to appear in search results.
        traverse_up: Permission to traverse from this block to ancestors.
        traverse_down: Permission to traverse from this block to descendants.
    """

    read: AccessRule = field(default_factory=AccessRule.allow)
    write: AccessRule = field(default_factory=AccessRule.allow)
    search: AccessRule = field(default_factory=AccessRule.allow)
    traverse_up: AccessRule = field(default_factory=AccessRule.allow)
    traverse_down: AccessRule = field(default_factory=AccessRule.allow)

    @classmethod
    def public(cls) -> "MemoryPolicy":
        """Create the default public policy.

        Returns:
            A policy that allows every operation to every principal.
        """

        return cls()
