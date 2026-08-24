"""MemoryOS's fast tier: a fixed-size queue of the newest exchanges.

Short-term memory is the simplest part of MemoryOS and the part that drives
everything else. It holds the last few exchanges verbatim, it has a hard
capacity, and when a new exchange arrives at a full queue the oldest one is
paged out rather than dropped. Every page in mid-term memory and every fact in
long-term memory started as an exchange that fell off this end.

A queue is arrival order, so this tier is an `EventLog`. Appending is one call,
the oldest entry is the first one the log reads back, and the sequence numbers
keep climbing because a log is append-only -- an exchange leaves the queue by
its block being deleted, not by the log forgetting it. That is the same shape as
upstream, where the deque is a window over a conversation that keeps going.
"""

from __future__ import annotations

from genie import MemoryBlock, MemoryHandle
from lamps.memoryos.model import (
    QA_TYPE,
    READ_LIMIT,
    Exchange,
    QAPair,
    exchange_from_json,
    format_timestamp,
    positive,
)
from lamps.memoryos.structures import MemoryOSStructures


class ShortTermMemory:
    """The FIFO queue of recent exchanges, with MemoryOS's capacity rule."""

    def __init__(self, memory: MemoryHandle, *, structures: MemoryOSStructures, capacity: int = 10) -> None:
        """Bind the queue to one user's short-term tier.

        Args:
            memory: Handle bounded to one user's memory.
            structures: Structure bundle for that memory.
            capacity: How many exchanges stay resident, `short_term_capacity`
                upstream.

        Raises:
            TypeError: If the capacity is not an integer.
            ValueError: If the capacity is not positive.
        """

        self.memory = memory
        self.structures = structures
        self.capacity = positive(capacity, field="capacity")

    def add(self, exchange: Exchange) -> QAPair:
        """Store one exchange at the newest end of the queue.

        The caller is expected to have made room first, the way upstream's
        `add_memory` runs the updater before appending. Nothing here evicts,
        because an eviction that happened quietly would be an exchange lost
        rather than an exchange promoted.

        Args:
            exchange: The user turn and the assistant's reply.

        Returns:
            The stored exchange with its queue position.
        """

        block = self.structures.tiers.create_child(
            parent=self.structures.short_term_root,
            type=QA_TYPE,
            value=exchange.as_json(),
            metadata={"tier": "short_term", "timestamp": format_timestamp(exchange.occurred_at)},
        )
        item = self.structures.queue.append(block)
        return QAPair(block=item.block, sequence=item.entry.sequence, exchange=exchange)

    def entries(self, *, limit: int = READ_LIMIT) -> list[QAPair]:
        """Return the resident exchanges, oldest first.

        Args:
            limit: Maximum entries to return.

        Returns:
            The queue in arrival order, which is what MemoryOS includes in a
            prompt in full.
        """

        return [_pair(item.block, item.entry.sequence) for item in self.structures.queue.read(limit=limit)]

    def size(self) -> int:
        """Return how many exchanges are resident.

        Returns:
            The queue length.
        """

        return len(self.entries())

    def is_full(self) -> bool:
        """Return whether the queue has reached its capacity.

        Returns:
            `True` when an arriving exchange would need room made first.
        """

        return self.size() >= self.capacity

    def oldest(self) -> QAPair | None:
        """Return the exchange that would be paged out next.

        Returns:
            The oldest resident exchange, or `None` when the queue is empty.
        """

        items = self.structures.queue.read(limit=1)
        return _pair(items[0].block, items[0].entry.sequence) if items else None

    def pop_oldest(self) -> QAPair | None:
        """Remove and return the oldest exchange.

        Deleting the block is what takes the entry out of the queue: the log
        keeps its sequence, and a read skips a position whose block is gone.

        Returns:
            The exchange that left the queue, or `None` when it was empty.
        """

        pair = self.oldest()
        if pair is None:
            return None
        self.memory.delete(pair.block)
        return pair


def _pair(block: MemoryBlock, sequence: int) -> QAPair:
    """Rebuild a queue entry from its block and its position in the log."""

    return QAPair(block=block, sequence=sequence, exchange=exchange_from_json(block.value))
