"""One read across all 4 tiers, which gives the context that MemoryOS puts in a prompt.

MemoryOS retrieves before it answers. For each query, it retrieves from all
tiers:

- **Short-term:** the full queue, word for word.
- **Mid-term:** the pages in the segments that match the query.
- **Long-term:** the entries from each knowledge base that match the query, and
  the current profile.

Notes:

- **Order:** upstream runs the 3 search parts in a thread pool. This changes the
  latency but not the result. Here they run one after the other.
- **Not read-only:** when retrieval reaches a mid-term segment, it records a
  visit. The visit raises the heat of the segment, and heat promotes the segment
  at a later time. So the queries of an agent help decide what its memory keeps.
  This is the mechanism, not a side effect.
- **End of the lamp:** upstream, the next step is the generation module. That
  module formats this context into a prompt and calls a model. The lamp stops
  before that step. `RetrievedContext` is the value that the generation module
  would format.
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
    """Collects context from the queue, the segments, and the knowledge bases."""

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
        """Bind a retriever to the tiers that it reads, with the MemoryOS limits.

        Args:
            short_term: The FIFO queue. The retriever reads all of it.
            mid_term: The paged tier. The retriever searches and visits it.
            long_term: The profile and the knowledge bases.
            queue_capacity: Maximum number of mid-term pages to keep,
                `retrieval_queue_capacity` upstream.
            segment_threshold: Minimum segment score for a search of its pages.
            page_threshold: Minimum page score to keep a page.
            top_k_segments: Maximum number of matched segments to search for
                pages.
            knowledge_threshold: Minimum knowledge score to keep an entry.
            top_k_knowledge: Maximum number of entries from each knowledge base.

        Raises:
            TypeError: If a limit has the wrong runtime type.
            ValueError: If a limit is out of range.
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
        """Read each tier for one query.

        Args:
            query: Free text to retrieve context for.
            now: Time of the retrieval. Each segment that the query reaches
                records this time as a visit.

        Returns:
            The context that MemoryOS would turn into a prompt.

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
