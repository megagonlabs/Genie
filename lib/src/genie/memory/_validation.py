"""Argument checks shared by memory records and access objects."""

from __future__ import annotations


def require_non_empty_str(value: object, *, field_name: str) -> str:
    """Require a non-empty string, such as an id, block type, or relation.

    Args:
        value: Value to check.
        field_name: Name used in the error message.

    Returns:
        `value`, unchanged.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If `value` is empty or contains only whitespace.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    return value
