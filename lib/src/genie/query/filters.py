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
"""Friendly filter operators, and the canonical names that adapters get."""

_CANONICAL_OPERATORS = frozenset(OPERATOR_ALIASES.values())
_SUPPORTED_OPERATORS_TEXT = ", ".join([*OPERATOR_ALIASES, *dict.fromkeys(OPERATOR_ALIASES.values())])
_BLOCK_FILTER_FIELDS = frozenset(field.name for field in fields(MemoryBlock))


def normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Return filters in which friendly operators have canonical names.

    Args:
        filters: Filter mapping from user code, or `None`.

    Returns:
        A new filter mapping. Aliases such as `">="` and `"in"` change to
        canonical operators such as `"$gte"` and `"$in"`. Nested operands are
        deep copies of the caller input. `None` gives an empty mapping.

    Raises:
        TypeError: If `filters` is not a dictionary or `None`, or a field name
            or an operator name is not a string.
        ValueError: If an operator is not a supported friendly or canonical
            operator name. So a typo such as `">>="` fails before any backend
            runs, and it does not return an empty result.
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
    """Return whether a block matches metadata or block-field filters.

    - **Filter forms:** an exact value, or a map of the operators in
      `OPERATOR_ALIASES`.
    - **Portable subset:** each shipped adapter uses this matcher. An adapter
      author can add richer filters in a custom backend.
    - **No match by default:** a filter key that is not a string, an empty
      operator map, an unknown operator, or a comparison that cannot run gives
      `False`.

    Args:
        block: Block to evaluate.
        filters: Normalized or user-facing filters.

    Returns:
        `True` when the block matches all filters.
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
    """Read a filter field from a block or its metadata.

    - **`metadata.` prefix:** reads the metadata key after the prefix.
    - **Declared field:** a `MemoryBlock` field name reads that field.
    - **Other names:** read metadata. A metadata key that does not exist gives `None`.
    """

    if key.startswith("metadata."):
        return block.metadata.get(key.removeprefix("metadata."))
    if key in _BLOCK_FILTER_FIELDS:
        return getattr(block, key)
    return block.metadata.get(key)


def _matches_operator(actual: Any, operators: dict[str, Any]) -> bool:
    """Evaluate operator filters against one value.

    - **Safe comparisons:** a comparison that cannot run returns `False`, so it
      does not match by mistake.
    - **Unknown operators:** `normalize_filters` rejects them before a query
      exists. This matcher still treats one as no match, for an adapter that
      gets a filter without normalization.
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
