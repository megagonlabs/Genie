"""Portable filter normalization and matching helpers."""

from __future__ import annotations

from typing import Any

from genie.memory.block import MemoryBlock

OPERATOR_ALIASES = {
    "==": "$eq",
    "=": "$eq",
    "!=": "$ne",
    ">": "$gt",
    ">=": "$gte",
    "<": "$lt",
    "<=": "$lte",
    "in": "$in",
}


def normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Return filters with human-friendly operators normalized.

    Args:
        filters: Filter mapping supplied by user code.

    Returns:
        A new filter mapping where aliases such as `">="` and `"in"` are
        rewritten to canonical adapter-facing operators such as `"$gte"` and
        `"$in"`.
    """

    normalized: dict[str, Any] = {}
    for key, expected in (filters or {}).items():
        if isinstance(expected, dict):
            normalized[key] = {
                OPERATOR_ALIASES.get(operator, operator): operand
                for operator, operand in expected.items()
            }
        else:
            normalized[key] = expected
    return normalized


def matches_filters(block: MemoryBlock, filters: dict[str, Any]) -> bool:
    """Match simple exact metadata or block-field filters.

    Adapter authors can implement richer filtering in their own backend. The
    in-memory backend intentionally supports a small portable subset.

    Args:
        block: Block to evaluate.
        filters: Normalized or user-facing filters.

    Returns:
        `True` when the block satisfies every filter.
    """

    for key, expected in filters.items():
        actual = _read_filter_value(block, key)
        if isinstance(expected, dict):
            if not _matches_operator(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def _read_filter_value(block: MemoryBlock, key: str) -> Any:
    """Read a filter field from a block or its metadata."""

    if key.startswith("metadata."):
        return block.metadata.get(key.removeprefix("metadata."))
    if hasattr(block, key):
        return getattr(block, key)
    return block.metadata.get(key)


def _matches_operator(actual: Any, operators: dict[str, Any]) -> bool:
    """Evaluate operator filters against one value.

    Incompatible comparisons return `False` instead of matching by mistake.
    """

    try:
        for op, expected in operators.items():
            op = OPERATOR_ALIASES.get(op, op)
            if op == "$eq":
                if actual != expected:
                    return False
            elif op == "$in":
                if actual not in expected:
                    return False
            elif op == "$ne":
                if actual == expected:
                    return False
            elif op == "$gt":
                if actual is None or actual <= expected:
                    return False
            elif op == "$gte":
                if actual is None or actual < expected:
                    return False
            elif op == "$lt":
                if actual is None or actual >= expected:
                    return False
            elif op == "$lte":
                if actual is None or actual > expected:
                    return False
            else:
                return False
    except TypeError:
        return False
    return True
