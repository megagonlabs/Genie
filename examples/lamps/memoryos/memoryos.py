"""The public interface of MemoryOS over the 4 tiers, the updater, and the retriever.

Upstream, this is the `Memoryos` class. Its interface is small:

- **`add_memory`:** give it an exchange.
- **`retrieve_memory`:** get context from it.
- **Profile:** read the profile that it inferred.
- **Stats:** see what each store holds.

All other behavior occurs because the caller called `add_memory`, not because
the caller asked for it: page-out, topic segments, heat, promotion, and
eviction.

This lamp keeps that property. So `add_memory` here returns the effects that it
started, and not nothing:

- **One call, many effects:** one call can page the oldest exchange out of the
  queue, open or extend a segment, evict the coldest segment, promote the
  hottest segment, and remove the oldest fact from a knowledge base to make
  room.
- **Visible hierarchy:** the result contains all of these effects. So a caller
  can inspect the hierarchy from the result alone.
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
    """The memory operating system of one user, connected to one assistant."""

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
        """Connect the 4 tiers, the updater, and the retriever.

        Args:
            memory: Handle bounded to the memory of one user.
            structures: Structure bundle for that memory.
            short_term: The FIFO queue.
            mid_term: The paged tier.
            long_term: The profile and the knowledge bases.
            updater: The part that moves dialogue between tiers.
            retriever: The part that reads each tier for a query.
        """

        self.memory = memory
        self.structures = structures
        self.short_term = short_term
        self.mid_term = mid_term
        self.long_term = long_term
        self.updater = updater
        self.retriever = retriever

    def add_memory(self, exchange: Exchange) -> AddResult:
        """Record one exchange, and run all steps that it starts.

        The order is the upstream order:

        - **Make room:** first, the updater pages out from a full queue, so no
          exchange is lost to an overflow.
        - **Store:** the queue stores the exchange.
        - **Check heat:** the updater promotes the hottest segment if its heat is
          at or above the heat threshold.

        Args:
            exchange: The user turn and the reply of the assistant, with the
                time when it occurred and what an analysis of it would conclude.

        Returns:
            The stored exchange and each tier movement that the call caused.
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
        """Read each tier for one query, and make the segments that it reaches hotter.

        Args:
            query: Free text to retrieve context for.
            now: Time of the retrieval.

        Returns:
            The context for a prompt.
        """

        return self.retriever.retrieve(query, now=now)

    def force_mid_term_analysis(self, *, now: datetime) -> Promotion | None:
        """Analyze the hottest segment, at any heat.

        Upstream sets the threshold to zero, runs the check, and sets the
        threshold back. A caller uses this to promote on demand, and does not
        wait until a segment becomes hot.

        Args:
            now: Time of the analysis.

        Returns:
            What the analysis promoted, or `None` when no segment exists.
        """

        return self.updater.promote_if_hot(now=now, threshold=0.0)

    def get_user_profile(self) -> ProfileRevision | None:
        """Return the profile that the analyses produced.

        Returns:
            The newest revision, or `None` when no promotion set profile traits
            yet.
        """

        return self.long_term.profile()

    def profile_history(self, *, limit: int = 100) -> list[ProfileRevision]:
        """Return each profile that an analysis wrote, oldest first.

        Args:
            limit: Maximum number of revisions to return.

        Returns:
            The revisions in order.
        """

        return self.long_term.profile_history(limit=limit)

    def knowledge(self, owner: KnowledgeOwner) -> list[KnowledgeItem]:
        """Return one full knowledge base.

        Args:
            owner: The owner of the knowledge base to read.

        Returns:
            The entries, oldest first.
        """

        return self.long_term.knowledge(owner)

    def segments(self, *, now: datetime) -> list[Segment]:
        """Return the open mid-term segments with their heat.

        Args:
            now: Time for the heat calculation.

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
            depth: Maximum number of edges to follow.

        Returns:
            The pages that continue it, nearest first.
        """

        return self.mid_term.chain_from(page_id, depth=positive(depth, field="depth"))

    def sources_of(self, block: MemoryBlock) -> list[Page]:
        """Return the pages that a promoted record came from.

        Args:
            block: A knowledge entry block or a profile revision block.

        Returns:
            The pages that are still in mid-term memory, in page id order. The
            list does not include a page that mid-term memory evicted, because
            Genie removes the links of a deleted block.
        """

        found: list[Page] = []
        for source in self.long_term.sources_of(block):
            page = self.mid_term.page(str(source.value["page_id"]))
            if page is not None:
                found.append(page)
        return found

    def memory_stats(self) -> dict[str, int]:
        """Return the quantity in each tier, as upstream `get_memory_stats` does.

        Returns:
            Counts for each tier, the capacities of the short-term and mid-term
            tiers, and the current revision number of the profile.
        """

        return {
            "short_term": self.short_term.size(),
            "short_term_capacity": self.short_term.capacity,
            "mid_term_sessions": self.mid_term.count(),
            "mid_term_capacity": self.mid_term.capacity,
            "mid_term_pages": self.mid_term.page_count(),
            **self.long_term.counts(),
        }
