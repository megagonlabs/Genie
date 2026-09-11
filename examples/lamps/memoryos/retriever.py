"""One read across all four tiers, which is what MemoryOS hands a prompt.

MemoryOS retrieves before it answers, and it retrieves from everywhere at once:

- the whole short-term queue verbatim;
- the mid-term pages whose segments match the query; and
- the matching entries from each knowledge base, with the current profile
  alongside.

Upstream runs the three searching halves in a thread pool, which changes the
latency and not the result. Here they run in order.

Retrieval is not read-only. Reaching a mid-term segment counts as a visit, which
raises its heat, which is what eventually promotes it. So the queries an agent
asks are part of what its memory decides to keep, and that is the mechanism, not
a side effect.

Retrieval is also where the lamp stops. The step after it upstream is the
generation module, which formats all of this into a prompt and calls a model.
`RetrievedContext` is exactly the value that would be formatted.
"""

from __future__ import annotations

from datetime import datetime

from examples.lamps.memoryos.long_term import LongTermMemory
from examples.lamps.memoryos.mid_term import MidTermMemory
from examples.lamps.memoryos.model import (
    ASSISTANT,
    USER,
    RetrievedContext,
    fraction,
    non_negative,
    normalize_timestamp,
    positive,
    required_text,
)
from examples.lamps.memoryos.short_term import ShortTermMemory


class Retriever:
    """Gathers context from the queue, the segments, and the knowledge bases."""

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        mid_term: MidTermMemory,
        long_term: LongTermMemory,
        queue_capacity: int = 7,
        segment_threshold: float = 0.1,
        page_threshold: float = 0.1,
        top_k_segments: int = 5,
        knowledge_threshold: float = 0.01,
        top_k_knowledge: int = 20,
    ) -> None:
        """Bind a retriever to the tiers it reads, with MemoryOS's own bounds.

        Args:
            short_term: The FIFO queue, read in full.
            mid_term: The paged tier, searched and visited.
            long_term: The profile and knowledge bases.
            queue_capacity: Mid-term pages kept, `retrieval_queue_capacity`
                upstream.
            segment_threshold: Minimum segment score to look inside.
            page_threshold: Minimum page score to keep.
            top_k_segments: How many matching segments to look inside.
            knowledge_threshold: Minimum knowledge score to keep.
            top_k_knowledge: Maximum entries per knowledge base.

        Raises:
            TypeError: If a bound has the wrong runtime type.
            ValueError: If a bound is out of range.
        """

        self.short_term = short_term
        self.mid_term = mid_term
        self.long_term = long_term
        self.queue_capacity = positive(queue_capacity, field="queue_capacity")
        self.segment_threshold = non_negative(segment_threshold, field="segment_threshold")
        self.page_threshold = fraction(page_threshold, field="page_threshold")
        self.top_k_segments = positive(top_k_segments, field="top_k_segments")
        self.knowledge_threshold = fraction(knowledge_threshold, field="knowledge_threshold")
        self.top_k_knowledge = positive(top_k_knowledge, field="top_k_knowledge")

    def retrieve(self, query: str, *, now: datetime) -> RetrievedContext:
        """Read every tier for one query.

        Args:
            query: Free text to retrieve for.
            now: Time of the retrieval, which becomes a visit on every segment
                the query reaches.

        Returns:
            The context MemoryOS would turn into a prompt.

        Raises:
            ValueError: If the query is blank.
        """

        text = required_text(query, field="query")
        moment = normalize_timestamp(now, field="now")
        pages = self.mid_term.search(
            text,
            now=moment,
            segment_threshold=self.segment_threshold,
            page_threshold=self.page_threshold,
            top_k_segments=self.top_k_segments,
        )
        current = self.long_term.profile()
        return RetrievedContext(
            query=text,
            retrieved_at=moment,
            short_term=tuple(self.short_term.entries()),
            pages=tuple(pages[: self.queue_capacity]),
            profile=dict(current.data) if current is not None else {},
            profile_revision=current.revision if current is not None else 0,
            user_knowledge=tuple(
                self.long_term.search_knowledge(USER, text, threshold=self.knowledge_threshold, top_k=self.top_k_knowledge)
            ),
            assistant_knowledge=tuple(
                self.long_term.search_knowledge(ASSISTANT, text, threshold=self.knowledge_threshold, top_k=self.top_k_knowledge)
            ),
        )
