from __future__ import annotations

from dataclasses import dataclass, field

from genie.policy.principal import Principal


@dataclass(frozen=True, slots=True)
class AccessRule:
    allow_all: bool = True
    principals: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def allow(cls) -> "AccessRule":
        return cls(allow_all=True)

    @classmethod
    def deny(cls) -> "AccessRule":
        return cls(allow_all=False)

    @classmethod
    def only(
        cls,
        *,
        principals: set[str] | frozenset[str] | None = None,
        roles: set[str] | frozenset[str] | None = None,
    ) -> "AccessRule":
        return cls(
            allow_all=False,
            principals=frozenset(principals or ()),
            roles=frozenset(roles or ()),
        )

    def allows(self, principal: Principal) -> bool:
        if self.allow_all:
            return True
        if principal.id in self.principals:
            return True
        return bool(self.roles.intersection(principal.roles))


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    read: AccessRule = field(default_factory=AccessRule.allow)
    write: AccessRule = field(default_factory=AccessRule.allow)
    search: AccessRule = field(default_factory=AccessRule.allow)
    traverse_up: AccessRule = field(default_factory=AccessRule.allow)
    traverse_down: AccessRule = field(default_factory=AccessRule.allow)

    @classmethod
    def public(cls) -> "MemoryPolicy":
        return cls()
