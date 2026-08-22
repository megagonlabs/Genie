"""Search scope values supported by Genie."""

from enum import StrEnum


class Scope(StrEnum):
    """Where a search operation should look for candidate blocks.

    A scope is resolved against the query's `root_id`. Every scope except
    `GLOBAL` needs one, and `MemoryHandle` further restricts which scopes a
    caller may use.

    Attributes:
        SELF: Only the root block itself.
        SUBTREE: The root block and everything it owns, at any depth.
        ANCESTORS: The root block and its parents up to the top of the tree.
        LINKS: Blocks connected to the root by a typed link, in either
            direction. Link membership does not imply ownership.
        GLOBAL: Every block, ignoring the root.
    """

    SELF = "self"
    SUBTREE = "subtree"
    ANCESTORS = "ancestors"
    LINKS = "links"
    GLOBAL = "global"
