"""Portable filter normalization and matching helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
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
"""Human-friendly filter operators and their canonical adapter-facing names."""

_CANONICAL_OPERATORS = frozenset(OPERATOR_ALIASES.values())
_SUPPORTED_OPERATORS_TEXT = ", ".join([*OPERATOR_ALIASES, *dict.fromkeys(OPERATOR_ALIASES.values())])
_BLOCK_FILTER_FIELDS = frozenset(field.name for field in fields(MemoryBlock))


def normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Return filters with human-friendly operators normalized.

    Args:
        filters: Filter mapping supplied by user code.

    Returns:
        A new filter mapping where aliases such as `">="` and `"in"` are
        rewritten to canonical adapter-facing operators such as `"$gte"` and
        `"$in"`. Nested operands are copied from caller-owned input.

    Raises:
        TypeError: If filters, field names, or operator names have invalid
            runtime types.
        ValueError: If an operator is not a supported friendly or canonical
            operator name, so a typo such as `">>="` fails before any backend
            runs instead of returning an empty result.
    """

    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise TypeError("filters must be a dictionary or None.")
    normalized: dict[str, Any] = {}
    for key, expected in filters.items():
        if not isinstance(key, str):
            raise TypeError("filter field names must be strings.")
        if isinstance(expected, dict):
            operators: dict[str, Any] = {}
            for operator, operand in expected.items():
                if not isinstance(operator, str):
                    raise TypeError("filter operator names must be strings.")
                canonical = OPERATOR_ALIASES.get(operator, operator)
                if canonical not in _CANONICAL_OPERATORS:
                    raise ValueError(f"filter operator {operator!r} on field {key!r} is not supported. Supported operators: {_SUPPORTED_OPERATORS_TEXT}.")
                operators[canonical] = deepcopy(operand)
            normalized[key] = operators
        else:
            normalized[key] = deepcopy(expected)
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
        if not isinstance(key, str):
            return False
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
    if key in _BLOCK_FILTER_FIELDS:
        return getattr(block, key)
    return block.metadata.get(key)


def _matches_operator(actual: Any, operators: dict[str, Any]) -> bool:
    """Evaluate operator filters against one value.

    Incompatible comparisons return `False` instead of matching by mistake.
    `normalize_filters` rejects unknown operators before a query is built; this
    matcher still treats one as no match in case an adapter receives a filter
    that skipped normalization.
    """

    if not operators:
        return False
    try:
        for op, expected in operators.items():
            if not isinstance(op, str):
                return False
            op = OPERATOR_ALIASES.get(op, op)
            if op == "$eq":
                if actual != expected:
                    return False
            elif op == "$in":
                if not isinstance(expected, list | tuple | set | frozenset):
                    return False
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
