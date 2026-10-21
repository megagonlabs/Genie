"""Search scope values that Genie supports."""

from enum import StrEnum


class Scope(StrEnum):
    """Where a search operation looks for candidate blocks.

    - **Root:** Genie resolves a scope against the `root_id` of the query.
      `Memory.search` raises `ValueError` if a scope other than `GLOBAL` has no
      root.
    - **Handles:** `MemoryHandle` puts more limits on the scopes that a caller
      can use.

    Attributes:
        SELF: Only the root block itself.
        SUBTREE: The root block and everything that it owns, at any depth.
        ANCESTORS: The root block and its parents up to the top of the tree.
        LINKS: The root block and the blocks that a typed link connects to
            it, in either direction. A mount link counts, so its target is in
            scope, but the subtree of the target is not. A link does not imply
            ownership.
        GLOBAL: All blocks. The root does not limit the candidates.
    """

    SELF = "self"
    SUBTREE = "subtree"
    ANCESTORS = "ancestors"
    LINKS = "links"
    GLOBAL = "global"
