from __future__ import annotations

from enum import StrEnum

from genie.memory.block import MemoryBlock
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal


class AccessDenied(PermissionError):
    """Raised when a principal cannot perform a memory operation."""


class Operation(StrEnum):
    READ = "read"
    WRITE = "write"
    SEARCH = "search"
    TRAVERSE_UP = "traverse_up"
    TRAVERSE_DOWN = "traverse_down"


class PolicyEngine:
    def require(
        self,
        operation: Operation | str,
        block: MemoryBlock,
        principal: Principal,
    ) -> None:
        if not self.allows(operation, block, principal):
            raise AccessDenied(
                f"Principal {principal.id!r} cannot {operation} memory block {block.id!r}."
            )

    def allows(
        self,
        operation: Operation | str,
        block: MemoryBlock,
        principal: Principal,
    ) -> bool:
        policy = block.policy or MemoryPolicy.public()
        rule: AccessRule = getattr(policy, Operation(operation).value)
        return rule.allows(principal)
