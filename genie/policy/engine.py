"""Policy evaluation for memory operations."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from genie.memory.block import MemoryBlock
from genie.policy.policy import (
    BUILTIN_OPERATION_NAMES,
    AccessRule,
    MemoryPolicy,
    _freeze_operation_rules,
)
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

    def __init__(self, *, operations: Mapping[str, AccessRule] | None = None) -> None:
        """Create a policy engine.

        Args:
            operations: Optional default rules for application-defined
                operations. Block-level `MemoryPolicy.operations` entries
                override these defaults for matching operation names.

        Raises:
            ValueError: If a custom operation attempts to override a built-in
                Genie operation.
        """

        self.operations: Mapping[str, AccessRule] = _freeze_operation_rules(
            {} if operations is None else operations
        )

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
            TypeError: If the block, principal, policy, or operation has an
                invalid runtime type.
            ValueError: If `operation` is neither built-in nor configured as a
                custom operation.
        """

        if not self.allows(operation, block, principal):
            raise AccessDenied(
                f"Principal {principal.id!r} cannot {_operation_name(operation)!r} "
                f"memory block {block.id!r}."
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
            TypeError: If the block, principal, policy, or operation has an
                invalid runtime type.
            ValueError: If `operation` is neither built-in nor configured as a
                custom operation.
        """

        if not isinstance(block, MemoryBlock):
            raise TypeError("block must be a MemoryBlock.")
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal.")
        policy = block.policy or MemoryPolicy.owner_private()
        if not isinstance(policy, MemoryPolicy):
            raise TypeError("MemoryBlock policy must be a MemoryPolicy or None.")
        rule = self._rule_for(policy, operation)
        return rule.allows(principal, owner_id=block.created_by)

    def _rule_for(self, policy: MemoryPolicy, operation: Operation | str) -> AccessRule:
        """Return the access rule that applies to an operation."""

        operation_name = _operation_name(operation)
        if operation_name in BUILTIN_OPERATION_NAMES:
            return getattr(policy, operation_name)
        rule = policy.operations.get(operation_name)
        if rule is not None:
            return rule
        rule = self.operations.get(operation_name)
        if rule is not None:
            return rule
        raise ValueError(
            f"Unknown policy operation {operation_name!r}. Configure it on "
            "`MemoryPolicy.operations` or `PolicyEngine(operations=...)`."
        )


def _operation_name(operation: Operation | str) -> str:
    """Return the canonical string name for a policy operation."""

    if isinstance(operation, Operation):
        return operation.value
    if not isinstance(operation, str):
        raise TypeError("operation must be an Operation or string.")
    if not operation:
        raise ValueError("operation must not be empty.")
    return operation
