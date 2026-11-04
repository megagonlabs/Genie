"""Shared store resolution for structure facades.

- **Binding:** facades bind to the backend that holds their structure records.
- **Composite backends:** when a caller gives a composite backend, these
  helpers resolve it to the route that owns the anchor block.
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

    - **Why:** a composite backend implements the core `MemoryBackend`
      contract and none of the optional structure capabilities. So it cannot
      hold structure records itself.
    - **Routing store:** when `store` routes blocks, this function returns the
      backend that owns `container`. The facade then binds to that adapter.
    - **Other stores:** this function returns them with no change.
    - **One route:** a container and its subtree always share one route. So the
      resolved backend owns each block that the structure can hold.
    - **Call order:** a facade calls this function after it gets the container
      through its access object. The route lookup has no handle or policy
      check. If it ran first, a rooted handle would raise `MemoryNotFound` for
      a missing id outside its root and `AccessDenied` for an id that exists.
      The error would then show whether the block exists.

    Args:
        store: Store or routing backend from the caller.
        container: Block or id that is the anchor of the structure.
        missing_message: Error text for the case in which no route owns the
            container.

    Returns:
        Store for the facade to bind to.

    Raises:
        MemoryNotFound: If `store` routes blocks and no route owns `container`.
    """

    if not isinstance(store, BlockRouter):
        return store
    resolved = store.backend_for_block(block_id(container))
    if resolved is None:
        raise MemoryNotFound(missing_message)
    return resolved
