"""The fast tier of MemoryOS: a queue with a fixed size that holds the newest exchanges.

Short-term memory is the simplest part of MemoryOS, and it drives all other
parts:

- **Word for word:** it holds the last few exchanges as they occurred, with a
  hard capacity.
- **Page-out, not drop:** when a new exchange arrives at a full queue, the oldest
  exchange moves out to mid-term memory. It is not dropped.
- **Source of all memory:** each page in mid-term memory and each fact in
  long-term memory started as an exchange that left this end of the queue.

A queue is arrival order, so this tier is an `EventLog`:

- **Append:** one call adds an exchange. The oldest entry is the first entry that
  the log reads back.
- **Sequence numbers only increase:** a log is append-only. An exchange leaves
  the queue when the lamp deletes its block. The log does not forget the entry.
- **Same shape as upstream:** upstream, the deque is a window over a
  conversation that continues.
"""

from __future__ import annotations

from examples.lamps.memoryos.model import (
    QA_TYPE,
    READ_LIMIT,
    Exchange,
    QAPair,
    exchange_from_json,
    format_timestamp,
    positive,
)
from examples.lamps.memoryos.structures import MemoryOSStructures
from genie import MemoryBlock, MemoryHandle


class ShortTermMemory:
    """The FIFO queue of recent exchanges, with the MemoryOS capacity rule."""

    def __init__(self, memory: MemoryHandle, *, structures: MemoryOSStructures, capacity: int = 10) -> None:
        """Bind the queue to the short-term tier of one user.

        Args:
            memory: Handle bounded to the memory of one user.
            structures: Structure bundle for that memory.
            capacity: Maximum number of exchanges in the queue,
                `short_term_capacity` upstream.

        Raises:
            TypeError: If the capacity is not an integer.
            ValueError: If the capacity is not positive.
        """

        self.memory = memory
        self.structures = structures
        self.capacity = positive(capacity, field="capacity")

    def add(self, exchange: Exchange) -> QAPair:
        """Store one exchange at the newest end of the queue.

        - **Caller makes room:** this method expects that the caller made room
          first. Upstream `add_memory` also runs the updater before it appends.
        - **No eviction here:** this method never evicts. A silent eviction would
          lose an exchange and not promote it.

        Args:
            exchange: The user turn and the reply of the assistant.

        Returns:
            The stored exchange with its position in the queue.
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
        """Return the exchanges in the queue, oldest first.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            The queue in arrival order. MemoryOS puts all of it in a prompt.
        """

        return [_pair(item.block, item.entry.sequence) for item in self.structures.queue.read(limit=limit)]

    def size(self) -> int:
        """Return the number of exchanges in the queue.

        Returns:
            The queue length.
        """

        return len(self.entries())

    def is_full(self) -> bool:
        """Return `True` if the queue is at its capacity.

        Returns:
            `True` when a new exchange needs room first.
        """

        return self.size() >= self.capacity

    def oldest(self) -> QAPair | None:
        """Return the exchange that the next page-out moves out.

        Returns:
            The oldest exchange in the queue, or `None` when the queue is empty.
        """

        items = self.structures.queue.read(limit=1)
        return _pair(items[0].block, items[0].entry.sequence) if items else None

    def pop_oldest(self) -> QAPair | None:
        """Remove and return the oldest exchange.

        The delete of the block removes the entry from the queue. The log keeps
        the sequence number, and a read skips a position whose block does not
        exist.

        Returns:
            The exchange that left the queue, or `None` when the queue was empty.
        """

        pair = self.oldest()
        if pair is None:
            return None
        self.memory.delete(pair.block)
        return pair


def _pair(block: MemoryBlock, sequence: int) -> QAPair:
    """Rebuild a queue entry from its block and its position in the log."""

    return QAPair(block=block, sequence=sequence, exchange=exchange_from_json(block.value))
