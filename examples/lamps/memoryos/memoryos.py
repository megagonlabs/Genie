"""MemoryOS's public surface over the four tiers and the two movers.

Upstream this is the `Memoryos` class, and its interface is small:

- Hand it an exchange with `add_memory`.
- Ask it for context with `retrieve_memory`.
- Read the profile it inferred.
- Look at what each store holds.

Everything else - paging out, segment grouping, heat, promotion, eviction -
happens because you called `add_memory`, not because you asked for it.

That is the part worth preserving, so `add_memory` here returns what it set in
motion rather than nothing. One call can page the oldest exchange out of the
queue, open or extend a segment, evict the coldest segment, promote the hottest
one, and drop the oldest fact from a knowledge base to make room. Returning all
of that is what makes a hierarchy inspectable instead of implied.
"""

from __future__ import annotations

from datetime import datetime

from examples.lamps.memoryos.long_term import LongTermMemory
from examples.lamps.memoryos.mid_term import MidTermMemory
from examples.lamps.memoryos.model import (
    AddResult,
    Eviction,
    Exchange,
    KnowledgeItem,
    KnowledgeOwner,
    Page,
    ProfileRevision,
    Promotion,
    RetrievedContext,
    Segment,
    normalize_timestamp,
    positive,
)
from examples.lamps.memoryos.retriever import Retriever
from examples.lamps.memoryos.short_term import ShortTermMemory
from examples.lamps.memoryos.structures import MemoryOSStructures
from examples.lamps.memoryos.updater import Updater
from genie import MemoryBlock, MemoryHandle


class MemoryOS:
    """One user's memory operating system, wired to one assistant."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: MemoryOSStructures,
        short_term: ShortTermMemory,
        mid_term: MidTermMemory,
        long_term: LongTermMemory,
        updater: Updater,
        retriever: Retriever,
    ) -> None:
        """Wire the four tiers, the updater, and the retriever together.

        Args:
            memory: Handle bounded to one user's memory.
            structures: Structure bundle for that memory.
            short_term: The FIFO queue.
            mid_term: The paged tier.
            long_term: The profile and knowledge bases.
            updater: What moves dialogue between tiers.
            retriever: What reads every tier for a query.
        """

        self.memory = memory
        self.structures = structures
        self.short_term = short_term
        self.mid_term = mid_term
        self.long_term = long_term
        self.updater = updater
        self.retriever = retriever

    def add_memory(self, exchange: Exchange) -> AddResult:
        """Record one exchange, and run whatever that sets in motion.

        The order is upstream's: make room in the queue first so nothing is lost
        to an overflow, store the exchange, then check whether the hottest
        segment has crossed the heat threshold.

        Args:
            exchange: The user turn and the assistant's reply, carrying the time
                it happened and what an analysis of it would conclude.

        Returns:
            The stored exchange and every tier movement the call caused.
        """

        now = normalize_timestamp(exchange.occurred_at, field="occurred_at")
        paged = self.updater.page_out(now=now)
        stored = self.short_term.add(exchange)
        promotion = self.updater.promote_if_hot(now=now)
        evictions: list[Eviction] = list(paged.evictions)
        if promotion is not None:
            evictions.extend(promotion.evictions)
        return AddResult(stored=stored, paged_out=paged.pages, promotion=promotion, evictions=tuple(evictions))

    def retrieve_memory(self, query: str, *, now: datetime) -> RetrievedContext:
        """Read every tier for one query, heating the segments it reaches.

        Args:
            query: Free text to retrieve for.
            now: Time of the retrieval.

        Returns:
            The context a prompt would be built from.
        """

        return self.retriever.retrieve(query, now=now)

    def force_mid_term_analysis(self, *, now: datetime) -> Promotion | None:
        """Analyze the hottest segment whatever its heat.

        Upstream this drops the threshold to zero, runs the check, and puts the
        threshold back, which is how a caller promotes on demand instead of
        waiting for a segment to warm up.

        Args:
            now: Time the analysis is taken at.

        Returns:
            What was promoted, or `None` when there is no segment at all.
        """

        return self.updater.promote_if_hot(now=now, threshold=0.0)

    def get_user_profile(self) -> ProfileRevision | None:
        """Return the profile the analyses have arrived at.

        Returns:
            The newest revision, or `None` when nothing has been promoted.
        """

        return self.long_term.profile()

    def profile_history(self, *, limit: int = 100) -> list[ProfileRevision]:
        """Return every profile an analysis has written, oldest first.

        Args:
            limit: Maximum revisions to return.

        Returns:
            The revisions in order.
        """

        return self.long_term.profile_history(limit=limit)

    def knowledge(self, owner: KnowledgeOwner) -> list[KnowledgeItem]:
        """Return one knowledge base in full.

        Args:
            owner: Whose knowledge base to read.

        Returns:
            The entries, oldest first.
        """

        return self.long_term.knowledge(owner)

    def segments(self, *, now: datetime) -> list[Segment]:
        """Return the open mid-term segments with their heat.

        Args:
            now: Time heat is being taken at.

        Returns:
            The segments in creation order.
        """

        return self.mid_term.segments(now=now)

    def page(self, page_id: str) -> Page | None:
        """Return one mid-term page by its id.

        Args:
            page_id: MemoryOS page id.

        Returns:
            The page, or `None` when it is not in mid-term memory.
        """

        return self.mid_term.page(page_id)

    def dialogue_chain(self, page_id: str, *, depth: int = 5) -> list[Page]:
        """Walk the dialogue chain forward from one page.

        Args:
            page_id: Page to start from.
            depth: Maximum edges to follow.

        Returns:
            The pages that continue it, nearest first.
        """

        return self.mid_term.chain_from(page_id, depth=positive(depth, field="depth"))

    def sources_of(self, block: MemoryBlock) -> list[Page]:
        """Return the pages a promoted record was extracted from.

        Args:
            block: A knowledge entry or profile revision block.

        Returns:
            The pages still held in mid-term memory, in page-id order. A page
            evicted from mid-term memory is not among them, because Genie drops
            a deleted block's links.
        """

        found: list[Page] = []
        for source in self.long_term.sources_of(block):
            page = self.mid_term.page(str(source.value["page_id"]))
            if page is not None:
                found.append(page)
        return found

    def memory_stats(self) -> dict[str, int]:
        """Return how much each tier holds, as upstream's `get_memory_stats`.

        Returns:
            Counts per tier, plus the profile's current revision number.
        """

        return {
            "short_term": self.short_term.size(),
            "short_term_capacity": self.short_term.capacity,
            "mid_term_sessions": self.mid_term.count(),
            "mid_term_capacity": self.mid_term.capacity,
            "mid_term_pages": self.mid_term.page_count(),
            **self.long_term.counts(),
        }
