"""Access control policy helpers."""

from typing import TYPE_CHECKING

from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal

if TYPE_CHECKING:
    from genie.policy.engine import AccessDenied, Operation, PolicyEngine

__all__ = [
    "AccessDenied",
    "AccessRule",
    "MemoryPolicy",
    "Operation",
    "PolicyEngine",
    "Principal",
]


def __getattr__(name: str):
    """Import policy engine objects on first use to avoid import cycles.

    Args:
        name: Exported attribute name.

    Returns:
        The requested policy engine object.

    Raises:
        AttributeError: If the name is not exported by this module.
    """

    if name in {"AccessDenied", "Operation", "PolicyEngine"}:
        from genie.policy.engine import AccessDenied, Operation, PolicyEngine

        exports = {
            "AccessDenied": AccessDenied,
            "Operation": Operation,
            "PolicyEngine": PolicyEngine,
        }
        return exports[name]
    raise AttributeError(name)
