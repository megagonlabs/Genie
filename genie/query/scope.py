"""Search scope values supported by Genie."""

from enum import StrEnum


class Scope(StrEnum):
    """Where a search operation should look for candidate blocks."""

    SELF = "self"
    SUBTREE = "subtree"
    ANCESTORS = "ancestors"
    LINKS = "links"
    GLOBAL = "global"
