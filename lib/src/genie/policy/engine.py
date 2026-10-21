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
    """Genie raises this `PermissionError` when a principal cannot perform a memory operation."""


class Operation(StrEnum):
    """Built-in operation names that the policy engine checks."""

    READ = "read"
    WRITE = "write"
    SEARCH = "search"
    TRAVERSE_UP = "traverse_up"
    TRAVERSE_DOWN = "traverse_down"


class PolicyEngine:
    """Evaluate block-level policies for a principal.

    - **Rule order:** a built-in field of the policy, then the entry in
      `MemoryPolicy.operations` of the block, then the engine default.
    - **No policy:** a block with `policy=None` uses
      `MemoryPolicy.owner_private()`.

    Attributes:
        operations: Default rules for application-defined operations. An entry
            with the same name in `MemoryPolicy.operations` of a block
            overrides the default.
    """

    def __init__(self, *, operations: Mapping[str, AccessRule] | None = None) -> None:
        """Create a policy engine.

        Args:
            operations: Optional default rules for application-defined
                operations. Block-level entries in `MemoryPolicy.operations`
                with the same names override these defaults.

        Raises:
            TypeError: If `operations` is not a mapping of string names to
                `AccessRule` values.
            ValueError: If a custom operation name is empty or is a built-in
                Genie operation name.
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
        """Raise `AccessDenied` if a principal cannot perform an operation.

        Args:
            operation: Operation to check.
            block: Block that has the policy.
            principal: Principal that tries the operation.

        Raises:
            AccessDenied: If the policy does not allow the operation.
            TypeError: If the block, principal, policy, or operation has a
                runtime type that is not valid.
            ValueError: If `operation` is empty, or is neither built-in nor
                configured as a custom operation.
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
        """Return whether a principal can perform an operation.

        Args:
            operation: Operation to check.
            block: Block that has the policy.
            principal: Principal that tries the operation.

        Returns:
            `True` when the policy allows the operation.

        Raises:
            TypeError: If the block, principal, policy, or operation has a
                runtime type that is not valid.
            ValueError: If `operation` is empty, or is neither built-in nor
                configured as a custom operation.
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
