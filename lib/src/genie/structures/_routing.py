"""Shared store resolution for structure facades.

Facades bind to the backend that holds their structure records. When a caller
supplies a composite backend instead, these helpers resolve it to the route that
owns the anchoring block.
"""

from __future__ import annotations

from typing import Any

from genie.backend.base import BlockRouter
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryNotFound
from genie.structures.access import BlockRef


def block_id(block: BlockRef) -> str:
    """Return the id for a block or a block id.

    Args:
        block: Block or block id.

    Returns:
        Block id.
    """

    return block.id if isinstance(block, MemoryBlock) else block


def resolve_store(store: Any, container: BlockRef, *, missing_message: str) -> Any:
    """Resolve a routing backend to the route that owns a container block.

    A composite backend implements the core `MemoryBackend` contract and none of
    the optional structure capabilities, so it cannot hold structure records
    itself. When `store` routes blocks, return the backend that owns `container`
    so the facade binds to the adapter that does. Any other store is returned
    unchanged.

    A container and its subtree always share one route, so the resolved backend
    owns every block the structure can hold.

    Args:
        store: Store or routing backend supplied by the caller.
        container: Block or id anchoring the structure.
        missing_message: Error text used when no route owns the container.

    Returns:
        Store the facade should bind to.

    Raises:
        MemoryNotFound: If `store` routes blocks and no route owns `container`.
    """

    if not isinstance(store, BlockRouter):
        return store
    resolved = store.backend_for_block(block_id(container))
    if resolved is None:
        raise MemoryNotFound(missing_message)
    return resolved
