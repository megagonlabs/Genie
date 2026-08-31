"""The two moves that make MemoryOS a hierarchy instead of three stores.

Everything interesting in MemoryOS happens at a tier boundary, and there are
only two boundaries to cross.

**Paging out.** When short-term memory is full, the oldest exchanges leave it
and are filed in mid-term memory under a themed summary, joining a segment about
the same thing when one is open. Consecutive exchanges that continue each other
are linked, so the dialogue chain survives the queue that held them - including
across two separate page-outs, which is why the updater remembers the last page
it wrote.

**Promotion.** After every write, the hottest segment is checked against a heat
threshold. If it is over the line its unread pages are analyzed:

- what they say about the user goes into the profile;
- what they say about the world goes into the two knowledge bases; and
- the pages are marked so they stop counting toward the segment's length.

The segment then cools, and something else gets a turn.

Two notes on both moves:

- They are copy-then-drop rather than a move, because Genie has no reparent
  operation. A page is written under its new owner and removed from its old one.
  That is also what upstream does, since the two tiers are two different stores.
- The analysis itself is two model calls upstream - one to rewrite the profile
  and one to split what was learned into user and assistant knowledge.
  `DeclaredAnalyst` replaces both by collecting what each exchange said it would
  contribute when it was recorded. That keeps the model's judgment at the
  boundary and makes every stored fact name the page it came from.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from lamps.memoryos.long_term import LongTermMemory
from lamps.memoryos.mid_term import MidTermMemory, SegmentWrite
from lamps.memoryos.model import (
    ASSISTANT,
    USER,
    Analysis,
    Eviction,
    Exchange,
    KnowledgeItem,
    KnowledgeOwner,
    Page,
    Promotion,
    QAPair,
    Segment,
    keywords,
    non_negative,
    normalize_timestamp,
)
from lamps.memoryos.short_term import ShortTermMemory


@dataclass(frozen=True, slots=True)
class PageOut:
    """What one overflow moved out of short-term memory.

    Attributes:
        evicted: The exchanges that left the queue, oldest first.
        writes: One write per themed group the batch was split into.
        pages: Every page written, in batch order.
        evictions: Segments the mid-term capacity rule dropped.
    """

    evicted: tuple[QAPair, ...]
    writes: tuple[SegmentWrite, ...]
    pages: tuple[Page, ...]
    evictions: tuple[Eviction, ...]


@dataclass(frozen=True, slots=True)
class DeclaredAnalyst:
    """Stands in for the two model calls promotion makes upstream.

    Upstream, promotion sends a hot segment's pages to a model twice: once to
    rewrite the user profile against the profile already on file, and once to
    split what the pages revealed into private user knowledge and assistant
    knowledge. Neither judgment can be reproduced without a model, so each
    exchange declares its own contribution when it is recorded, and this analyst
    collects the contributions of the pages being analyzed.

    The collection is not nothing:

    - the order decides which of two traits wins;
    - repeated facts are written once; and
    - every fact keeps the page that declared it, which is the provenance
      upstream discards.
    """

    def analyze(self, segment: Segment, pages: Sequence[Page]) -> Analysis:
        """Return what an analysis of these pages would write.

        Args:
            segment: Segment being analyzed. A model-backed analyst would read
                its summary; the declared one does not need to.
            pages: Its pages that no analysis has read yet, in dialogue order.

        Returns:
            The profile traits and knowledge entries to promote, each fact
            paired with the page it came from.
        """

        traits: dict[str, str] = {}
        user_facts: dict[str, str] = {}
        assistant_facts: dict[str, str] = {}
        for page in pages:
            insight = page.exchange.insight
            traits.update(insight.profile)
            for fact in insight.user_knowledge:
                user_facts.setdefault(fact, page.page_id)
            for fact in insight.assistant_knowledge:
                assistant_facts.setdefault(fact, page.page_id)
        return Analysis(
            profile=traits,
            user_knowledge=tuple(user_facts.items()),
            assistant_knowledge=tuple(assistant_facts.items()),
            source_page_ids=tuple(page.page_id for page in pages),
        )


class Updater:
    """Moves dialogue up the hierarchy: out of the queue, then into the profile."""

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        mid_term: MidTermMemory,
        long_term: LongTermMemory,
        analyst: DeclaredAnalyst | None = None,
        heat_threshold: float = 5.0,
    ) -> None:
        """Wire the updater to the three tiers it moves things between.

        Args:
            short_term: The FIFO queue.
            mid_term: The paged tier.
            long_term: The profile and knowledge bases.
            analyst: What stands in for the promotion model calls.
            heat_threshold: Heat a segment must reach to be analyzed,
                `mid_term_heat_threshold` upstream.

        Raises:
            TypeError: If the threshold is not a number.
            ValueError: If the threshold is negative.
        """

        self.short_term = short_term
        self.mid_term = mid_term
        self.long_term = long_term
        self.analyst = analyst if analyst is not None else DeclaredAnalyst()
        self.heat_threshold = non_negative(heat_threshold, field="heat_threshold")
        self._last_page: Page | None = None

    def page_out(self, *, now: datetime) -> PageOut:
        """Move the oldest exchanges out of a full queue into mid-term memory.

        This is upstream's `process_short_term_to_mid_term`. The queue is drained
        until it has room, the batch is split into themed groups the way the
        multi-summary call splits it, each group is filed under the segment that
        fits it, and consecutive exchanges that continue each other are chained.

        Args:
            now: Time the write is being made at.

        Returns:
            What left the queue and where it went. Every field is empty when the
            queue was not full.
        """

        moment = normalize_timestamp(now, field="now")
        evicted: list[QAPair] = []
        while self.short_term.is_full():
            pair = self.short_term.pop_oldest()
            if pair is None:
                break
            evicted.append(pair)
        if not evicted:
            return PageOut(evicted=(), writes=(), pages=(), evictions=())
        writes: list[SegmentWrite] = []
        pages: list[Page] = []
        evictions: list[Eviction] = []
        for group in _themed_groups(entry.exchange for entry in evicted):
            write = self.mid_term.insert_pages(
                group,
                summary=group[0].topic,
                summary_keywords=sorted(keywords(group[0].topic)),
                now=moment,
            )
            writes.append(write)
            evictions.extend(write.evictions)
            for page in write.pages:
                self._chain(page)
                pages.append(page)
        return PageOut(evicted=tuple(evicted), writes=tuple(writes), pages=tuple(pages), evictions=tuple(evictions))

    def promote_if_hot(self, *, now: datetime, threshold: float | None = None) -> Promotion | None:
        """Analyze the hottest segment when it is over the heat threshold.

        This is upstream's `_trigger_profile_and_knowledge_update_if_needed`,
        which runs after every write. Only the hottest segment is considered, so
        promotion is a trickle rather than a sweep.

        Args:
            now: Time the check is being made at.
            threshold: Heat to require instead of the configured one, which is
                how upstream's `force_mid_term_analysis` promotes on demand.

        Returns:
            What was promoted, or `None` when nothing was hot enough.
        """

        moment = normalize_timestamp(now, field="now")
        floor = self.heat_threshold if threshold is None else non_negative(threshold, field="threshold")
        segment = self.mid_term.hottest(now=moment)
        if segment is None or segment.heat < floor:
            return None
        return self.promote(segment, now=moment, threshold=floor)

    def promote(self, segment: Segment, *, now: datetime, threshold: float | None = None) -> Promotion:
        """Analyze one segment's unread pages and write what they revealed.

        The pages are marked analyzed whether or not the analysis found
        anything, and then the segment cools. That order matters: a segment that
        stayed hot after being read would be analyzed again on the next write and
        never let anything else through.

        Args:
            segment: Segment to analyze.
            now: Time the analysis finished.
            threshold: Heat the check required, recorded for readability.

        Returns:
            What the analysis wrote, and the heat the segment cooled to.
        """

        moment = normalize_timestamp(now, field="now")
        unread = tuple(page for page in segment.pages if not page.analyzed)
        analysis = self.analyst.analyze(segment, unread)
        revision = self.long_term.update_profile(analysis.profile, now=moment, source_page_ids=analysis.source_page_ids)
        evictions: list[Eviction] = []
        user_written = tuple(self._write_knowledge(USER, analysis.user_knowledge, now=moment, evictions=evictions))
        assistant_written = tuple(self._write_knowledge(ASSISTANT, analysis.assistant_knowledge, now=moment, evictions=evictions))
        self.mid_term.mark_analyzed(unread)
        cooled = self.mid_term.cool(segment, now=moment)
        return Promotion(
            segment_id=segment.segment_id,
            heat=segment.heat,
            threshold=self.heat_threshold if threshold is None else threshold,
            analyzed_page_ids=tuple(page.page_id for page in unread),
            profile_revision=revision.revision if revision is not None else 0,
            user_knowledge=user_written,
            assistant_knowledge=assistant_written,
            cooled_heat=cooled.heat,
            evictions=tuple(evictions),
        )

    def _write_knowledge(
        self,
        owner: KnowledgeOwner,
        facts: Sequence[tuple[str, str]],
        *,
        now: datetime,
        evictions: list[Eviction],
    ) -> list[KnowledgeItem]:
        """Append promoted facts to one knowledge base, collecting what it dropped."""

        written: list[KnowledgeItem] = []
        for fact, page_id in facts:
            item, eviction = self.long_term.add_knowledge(owner, fact, now=now, source_page_ids=(page_id,))
            written.append(item)
            if eviction is not None:
                evictions.append(eviction)
        return written

    def _chain(self, page: Page) -> None:
        """Link a page to the one before it when the dialogue continued."""

        previous = self._last_page
        self._last_page = page
        if previous is None or not page.exchange.continues:
            return
        if self.mid_term.page(previous.page_id) is None:
            return
        self.mid_term.link_pages(previous, page)


def _themed_groups(exchanges: Iterable[Exchange]) -> tuple[tuple[Exchange, ...], ...]:
    """Split a paged-out batch into runs that share a themed summary.

    Upstream a model reads the whole batch and returns several summaries, each
    with the pages that belong to it. Each exchange already carries the summary
    a model wrote for it, so grouping is a matter of keeping equal topics
    together instead of guessing them again.

    Args:
        exchanges: Exchanges in the order they left the queue.

    Returns:
        One group per distinct topic, in first-seen order.
    """

    grouped: dict[str, list[Exchange]] = {}
    for exchange in exchanges:
        grouped.setdefault(exchange.topic, []).append(exchange)
    return tuple(tuple(group) for group in grouped.values())
