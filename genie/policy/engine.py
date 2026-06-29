"""Policy evaluation for memory operations."""

from __future__ import annotations

from enum import StrEnum

from genie.memory.block import MemoryBlock
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal


class AccessDenied(PermissionError):
    """Raised when a principal cannot perform a memory operation."""


class Operation(StrEnum):
    """Operation names checked by the policy engine."""

    READ = "read"
    WRITE = "write"
    SEARCH = "search"
    TRAVERSE_UP = "traverse_up"
    TRAVERSE_DOWN = "traverse_down"


class PolicyEngine:
    """Evaluate block-level policies for a principal."""

    def require(
        self,
        operation: Operation | str,
        block: MemoryBlock,
        principal: Principal,
    ) -> None:
        """Require that a principal may perform an operation.

        Args:
            operation: Operation to check.
            block: Block carrying the policy.
            principal: Principal attempting the operation.

        Raises:
            AccessDenied: If the policy does not allow the operation.
            ValueError: If `operation` is not a valid `Operation`.
        """

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
        """Return whether a principal may perform an operation.

        Args:
            operation: Operation to check.
            block: Block carrying the policy.
            principal: Principal attempting the operation.

        Returns:
            `True` when the operation is allowed.

        Raises:
            ValueError: If `operation` is not a valid `Operation`.
        """

        policy = block.policy or MemoryPolicy.public()
        rule: AccessRule = getattr(policy, Operation(operation).value)
        return rule.allows(principal)
