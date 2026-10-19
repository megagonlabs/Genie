"""The 2 moves that make MemoryOS a hierarchy and not 3 separate stores.

All important behavior in MemoryOS occurs at a tier boundary. There are only 2
boundaries.

**Page-out.** When short-term memory is full, the oldest exchanges leave it.
The updater files them in mid-term memory under a themed summary. If a segment
about the same subject is open, they join that segment.

- **Dialogue chain:** the updater links consecutive exchanges that continue each
  other. So the chain stays after the queue that held them moves on.
- **Across page-outs:** the chain also links pages from 2 separate page-outs.
  For this reason, the updater remembers the last page that it wrote.

**Promotion.** After each write, the updater compares the hottest segment with a
heat threshold. If the heat is at or above the threshold, the updater analyzes
the unread pages of the segment:

- **Profile:** what the pages tell about the user goes into the profile.
- **Knowledge:** the facts that the pages declare go into the 2 knowledge bases.
- **Marks:** the updater marks the pages, so they no longer count toward the
  length of the segment.

Then the segment cools, and another segment can become the hottest.

Notes:

- **A page-out is a delete and a copy, not a move:** Genie has no reparent
  operation. So the queue first deletes the exchange under its old owner. Then
  the updater writes a page with the same exchange under its new owner. Upstream
  does the same, because the 2 tiers are 2 different stores.
- **Promotion is a copy only:** the analysis writes new long-term records. The
  pages stay in mid-term memory, marked as analyzed.
- **No model:** upstream, the analysis is 2 model calls. One call writes the
  profile again. The other call splits what the pages showed into user
  knowledge and assistant knowledge. `DeclaredAnalyst` replaces both calls. It
  collects the contribution that each exchange declared when the lamp recorded
  it. So the judgment of the model stays at the boundary, and each stored fact
  names the page that it came from.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from examples.lamps.memoryos.long_term import LongTermMemory
from examples.lamps.memoryos.mid_term import MidTermMemory, SegmentWrite
from examples.lamps.memoryos.model import (
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
from examples.lamps.memoryos.short_term import ShortTermMemory


@dataclass(frozen=True, slots=True)
class PageOut:
    """What one overflow moved out of short-term memory.

    Attributes:
        evicted: The exchanges that left the queue, oldest first.
        writes: One write for each themed group of the batch.
        pages: All pages that the page-out wrote, in write order: one themed
            group after the other.
        evictions: Segments that the mid-term capacity rule removed.
    """

    evicted: tuple[QAPair, ...]
    writes: tuple[SegmentWrite, ...]
    pages: tuple[Page, ...]
    evictions: tuple[Eviction, ...]


@dataclass(frozen=True, slots=True)
class DeclaredAnalyst:
    """Replaces the 2 model calls that promotion makes upstream.

    - **Upstream:** promotion sends the pages of a hot segment to a model 2
      times. The first call writes the user profile again, from the current
      profile. The second call splits what the pages showed into private user
      knowledge and assistant knowledge.
    - **Here:** a lamp without a model cannot make these judgments. So each
      exchange declares its own contribution when the lamp records it. This
      analyst collects the contributions of the analyzed pages.

    The collection still has rules:

    - **Order:** a later page replaces an earlier trait with the same name.
    - **No repeats:** the analyst writes a repeated fact only once, with the
      first page that declared it.
    - **Provenance:** each fact keeps the page that declared it. Upstream does
      not keep this provenance.
    """

    def analyze(self, segment: Segment, pages: Sequence[Page]) -> Analysis:
        """Return what an analysis of these pages would write.

        Args:
            segment: Segment that the analysis reads. An analyst with a model
                would read its summary. The declared analyst does not use it.
            pages: The pages of the segment that no analysis has read, in
                dialogue order.

        Returns:
            The profile traits and knowledge entries to promote. Each fact has
            the id of the page that it came from.
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
    """Moves dialogue up the hierarchy: out of the queue, and then into the profile and the knowledge bases."""

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        mid_term: MidTermMemory,
        long_term: LongTermMemory,
        analyst: DeclaredAnalyst | None = None,
        heat_threshold: float = 5.0,
    ) -> None:
        """Connect the updater to the 3 tiers that it moves data between.

        Args:
            short_term: The FIFO queue.
            mid_term: The paged tier.
            long_term: The profile and the knowledge bases.
            analyst: The replacement for the promotion model calls. `None`
                selects a new `DeclaredAnalyst`.
            heat_threshold: Minimum heat for an analysis of a segment,
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

        This is upstream `process_short_term_to_mid_term`:

        - **Drain:** the updater removes exchanges from the queue until the
          queue has room.
        - **Group:** it splits the batch into themed groups, as the upstream
          multi-summary call does.
        - **File:** it files each group under the segment that matches it.
        - **Chain:** it links consecutive exchanges that continue each other.

        Args:
            now: Time of the write.

        Returns:
            What left the queue and where it went. All fields are empty when the
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
        """Analyze the hottest segment when its heat is at or above the threshold.

        This is upstream `_trigger_profile_and_knowledge_update_if_needed`,
        which runs after each write. The method examines only the hottest
        segment. So each check promotes one segment at most, not all hot
        segments at once.

        Args:
            now: Time of the check.
            threshold: Heat to require in place of the configured threshold.
                Upstream `force_mid_term_analysis` uses this to promote on
                demand.

        Returns:
            What the analysis promoted, or `None` when no segment was hot
            enough.
        """

        moment = normalize_timestamp(now, field="now")
        floor = self.heat_threshold if threshold is None else non_negative(threshold, field="threshold")
        segment = self.mid_term.hottest(now=moment)
        if segment is None or segment.heat < floor:
            return None
        return self.promote(segment, now=moment, threshold=floor)

    def promote(self, segment: Segment, *, now: datetime, threshold: float | None = None) -> Promotion:
        """Analyze the unread pages of one segment, and write what they showed.

        - **Always mark:** the updater marks the pages as analyzed, also when
          the analysis found nothing. Then the segment cools.
        - **Why the order is important:** a segment that stays hot after the
          analysis would get a new analysis on the next write. Then no other
          segment could get a promotion.

        Args:
            segment: Segment to analyze.
            now: Time when the analysis finished.
            threshold: Heat that the check required. The result records it for
                readers. `None` records the configured threshold.

        Returns:
            What the analysis wrote, and the heat of the segment after it
            cooled.
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
        """Append promoted facts to one knowledge base, and collect the entries that it removed."""

        written: list[KnowledgeItem] = []
        for fact, page_id in facts:
            item, eviction = self.long_term.add_knowledge(owner, fact, now=now, source_page_ids=(page_id,))
            written.append(item)
            if eviction is not None:
                evictions.append(eviction)
        return written

    def _chain(self, page: Page) -> None:
        """Link a page to the page before it when the dialogue continued.

        The page before is the last page that this updater wrote. If that page
        is no longer in mid-term memory, the method adds no link.
        """

        previous = self._last_page
        self._last_page = page
        if previous is None or not page.exchange.continues:
            return
        if self.mid_term.page(previous.page_id) is None:
            return
        self.mid_term.link_pages(previous, page)


def _themed_groups(exchanges: Iterable[Exchange]) -> tuple[tuple[Exchange, ...], ...]:
    """Split a paged-out batch into groups that share a themed summary.

    - **Upstream:** a model reads the full batch and returns some summaries,
      each with the pages that belong to it.
    - **Here:** each exchange already has the summary that a model wrote for it.
      So the function puts exchanges with equal topics together, and does not
      guess the topics again. Exchanges with the same topic go into one group,
      also when they are not adjacent.

    Args:
        exchanges: Exchanges in the order that they left the queue.

    Returns:
        One group for each distinct topic, in the order of first occurrence.
    """

    grouped: dict[str, list[Exchange]] = {}
    for exchange in exchanges:
        grouped.setdefault(exchange.topic, []).append(exchange)
    return tuple(tuple(group) for group in grouped.values())
