"""MemoryOS's paged tier: topic segments, their pages, and their heat.

Mid-term memory is where MemoryOS stops being a transcript and starts being a
memory system. A page arriving from short-term memory is not filed by time; it
is filed with the other pages about the same thing, in a *session* that
MemoryOS also calls a segment. A segment carries a score:

    H_segment = alpha * N_visit + beta * L_interaction + gamma * R_recency

and that score is the whole promotion policy. Retrieval raises `N_visit`,
paging in more dialogue raises `L_interaction`, and time lowers `R_recency`. A
segment that keeps coming up gets hot enough to be read into long-term memory;
one that never comes up cools, and is eventually the one evicted when the tier
runs out of room.

Two of those three terms are read out of the store here rather than kept in step
with it by hand:

- `L_interaction` is the number of pages the segment owns that no analysis has
  read yet. That is exactly what upstream's counter holds, since it resets to
  zero at the same moment it marks every page analyzed.
- `R_recency` decays the last-visit time held in a `TemporalCollection`.
- So heat cannot drift away from the pages it is supposed to describe. There is
  no cached number to go stale, only a formula over what is stored.

The pages are the segment's children, which is the other thing ownership buys.
Evicting a segment is one `delete_subtree`, and its pages and their
dialogue-chain edges go with it. Upstream has to walk the pages it is about to
drop and clear the `pre_page`/`next_page` fields that point at them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import MemoryAddress
from lamps.memoryos.model import (
    PAGE_NAMESPACE,
    PAGE_TYPE,
    READ_LIMIT,
    RECENCY_TAU_HOURS,
    SEGMENT_NAMESPACE,
    SEGMENT_TYPE,
    Eviction,
    Exchange,
    Page,
    PageHit,
    Segment,
    exchange_from_json,
    format_timestamp,
    fraction,
    jaccard,
    keywords,
    new_id,
    non_negative,
    normalize_timestamp,
    overlap,
    parse_timestamp,
    positive,
    required_text,
    text_tuple,
    time_decay,
    tokenize,
)
from lamps.memoryos.structures import MemoryOSStructures


@dataclass(frozen=True, slots=True)
class SegmentWrite:
    """What one batch of paged-out exchanges did to mid-term memory.

    Attributes:
        segment: The segment the pages ended up in.
        pages: The pages that were written, in batch order.
        opened: Whether a new segment was opened for them.
        score: How well the batch matched the segment it joined, or zero when
            nothing was close enough and a segment was opened instead.
        evictions: Anything the capacity rule dropped to make room.
    """

    segment: Segment
    pages: tuple[Page, ...]
    opened: bool
    score: float
    evictions: tuple[Eviction, ...]


class MidTermMemory:
    """Topic segments, the pages they own, and the heat that ranks them."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: MemoryOSStructures,
        capacity: int = 2_000,
        similarity_threshold: float = 0.6,
        keyword_alpha: float = 1.0,
        tau_hours: float = RECENCY_TAU_HOURS,
    ) -> None:
        """Bind the paged tier to one user's mid-term store.

        Args:
            memory: Handle bounded to one user's memory.
            structures: Structure bundle for that memory.
            capacity: How many segments the tier holds, `mid_term_capacity`
                upstream.
            similarity_threshold: How well a batch must match a segment to join
                it instead of opening one, `mid_term_similarity_threshold`
                upstream.
            keyword_alpha: Weight on the keyword term of a match, upstream's
                `keyword_similarity_alpha`.
            tau_hours: Recency half-life used by the heat formula.

        Raises:
            TypeError: If a bound has the wrong runtime type.
            ValueError: If a bound is out of range.
        """

        self.memory = memory
        self.structures = structures
        self.capacity = positive(capacity, field="capacity")
        self.similarity_threshold = non_negative(similarity_threshold, field="similarity_threshold")
        self.keyword_alpha = non_negative(keyword_alpha, field="keyword_alpha")
        self.tau_hours = non_negative(tau_hours, field="tau_hours") or RECENCY_TAU_HOURS

    def insert_pages(
        self,
        exchanges: Sequence[Exchange],
        *,
        summary: str,
        summary_keywords: Sequence[str] = (),
        now: datetime,
    ) -> SegmentWrite:
        """File a batch of paged-out exchanges under the segment that fits.

        This is upstream's `insert_pages_into_session`. The batch arrives with
        the themed summary a model wrote for it, that summary is compared with
        every segment already open, and the best match above the threshold takes
        the pages. When nothing is close enough a segment is opened, which is
        `add_session`.

        Args:
            exchanges: Exchanges that fell out of short-term memory.
            summary: Themed summary the batch was grouped under.
            summary_keywords: Keywords that came with the summary. Content words
                from the summary are used when none are supplied.
            now: Time the write is being made at.

        Returns:
            The segment, the pages written into it, and anything the capacity
            rule evicted to make room.

        Raises:
            ValueError: If the batch is empty or the summary is blank.
        """

        if not exchanges:
            raise ValueError("insert_pages needs at least one exchange.")
        text = required_text(summary, field="summary")
        moment = normalize_timestamp(now, field="now")
        declared = text_tuple(summary_keywords, field="summary_keywords")
        terms = tuple(declared) if declared else tuple(sorted(keywords(text)))
        target, score = self._best_match(text, frozenset(terms), now=moment)
        evictions: tuple[Eviction, ...] = ()
        if target is None:
            block = self._open_segment(text, terms, now=moment)
            opened = True
        else:
            block = target.block
            opened = False
        pages = tuple(self._write_page(block, exchange) for exchange in exchanges)
        self._touch(block, now=moment)
        if opened:
            evictions = self._enforce_capacity(keep=block.id)
        return SegmentWrite(
            segment=self._read_segment(self._require_block(block.id), now=moment),
            pages=pages,
            opened=opened,
            score=round(score, 4),
            evictions=evictions,
        )

    def visit(self, segment: Segment, *, now: datetime) -> Segment:
        """Record that retrieval reached this segment, and reheat it.

        A read is a write in MemoryOS: reaching a segment raises `N_visit`,
        raises the counter eviction reads, and moves the last-visit time that
        the recency term decays from. Retrieval is therefore what decides what
        eventually reaches long-term memory.

        Args:
            segment: Segment that matched a query.
            now: Time of the retrieval.

        Returns:
            The segment as it now stands, with its new heat.
        """

        moment = normalize_timestamp(now, field="now")
        self.memory.update(
            segment.block,
            value={"N_visit": segment.visits + 1, "access_count_lfu": segment.lfu_count + 1},
        )
        self._touch(segment.block, now=moment)
        return self._read_segment(self._require_block(segment.id), now=moment)

    def mark_analyzed(self, pages: Sequence[Page]) -> tuple[Page, ...]:
        """Mark pages as read by an analysis, which drops them out of length.

        Args:
            pages: Pages the analysis read.

        Returns:
            The pages as they now stand.
        """

        marked: list[Page] = []
        for page in pages:
            block = self.memory.update(page.block, value={"analyzed": True}, metadata={"analyzed": True})
            marked.append(_read_page(block))
        return tuple(marked)

    def cool(self, segment: Segment, *, now: datetime) -> Segment:
        """Reset a segment's visit term after its pages have been promoted.

        Upstream sets `N_visit` and `L_interaction` to zero, refreshes the
        last-visit time, and recomputes heat. Only the visit count is written
        here; length falls on its own, because the pages the analysis just
        marked no longer count toward it.

        Args:
            segment: Segment that was analyzed.
            now: Time the analysis finished.

        Returns:
            The cooled segment.
        """

        moment = normalize_timestamp(now, field="now")
        self.memory.update(segment.block, value={"N_visit": 0})
        self._touch(segment.block, now=moment)
        return self._read_segment(self._require_block(segment.id), now=moment)

    def segments(self, *, now: datetime) -> list[Segment]:
        """Return every open segment, oldest first.

        Args:
            now: Time heat is being taken at.

        Returns:
            The segments in creation order.
        """

        moment = normalize_timestamp(now, field="now")
        return [self._read_segment(block, now=moment) for block in self._segment_blocks()]

    def count(self) -> int:
        """Return how many segments are open.

        Returns:
            The number of segments in the tier.
        """

        return len(self._segment_blocks())

    def page_count(self) -> int:
        """Return how many pages the tier holds across every segment.

        Returns:
            The number of pages, analyzed or not.
        """

        return sum(len(self._page_blocks(block)) for block in self._segment_blocks())

    def segment(self, segment_id: str, *, now: datetime) -> Segment | None:
        """Return one segment by its session id.

        Args:
            segment_id: MemoryOS session id.
            now: Time heat is being taken at.

        Returns:
            The segment, or `None` when no session has that id.
        """

        block = self.structures.ids.get(MemoryAddress(SEGMENT_NAMESPACE, required_text(segment_id, field="segment_id")))
        if block is None:
            return None
        return self._read_segment(block, now=normalize_timestamp(now, field="now"))

    def page(self, page_id: str) -> Page | None:
        """Return one page by its page id.

        Upstream this walks every session's page list until it finds a match.
        A page id is an address here, so the answer is one read that does not
        get slower as the tier fills.

        Args:
            page_id: MemoryOS page id.

        Returns:
            The page, or `None` when no page has that id.
        """

        block = self.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, required_text(page_id, field="page_id")))
        return None if block is None else _read_page(block)

    def hottest(self, *, now: datetime) -> Segment | None:
        """Return the segment the promotion check would look at first.

        Args:
            now: Time heat is being taken at.

        Returns:
            The hottest segment, or `None` when the tier is empty. Ties break
            on creation time and then session id so runs agree.
        """

        found = self.segments(now=now)
        if not found:
            return None
        return max(found, key=lambda segment: (segment.heat, -segment.created_at.timestamp(), segment.segment_id))

    def search(
        self,
        query: str,
        *,
        now: datetime,
        segment_threshold: float = 0.1,
        page_threshold: float = 0.1,
        top_k_segments: int = 5,
    ) -> list[PageHit]:
        """Return the pages that match a query, visiting the segments on the way.

        - Candidate selection is Genie's. The mid-term tier becomes a subtree
          scope, the block type narrows it to segments, and one search runs per
          query term.
        - Scoring is the lamp's, because Genie has no similarity measure. A
          segment scores the share of query terms that appear in its summary,
          plus MemoryOS's keyword Jaccard term. A page inside a qualifying
          segment scores the share of query terms in its own dialogue.

        Args:
            query: Free text to retrieve for.
            now: Time of the retrieval, recorded as a visit.
            segment_threshold: Minimum segment score to look inside.
            page_threshold: Minimum page score to keep.
            top_k_segments: How many matching segments to look inside.

        Returns:
            Page hits in descending score order, ties broken on page time and
            then page id.

        Raises:
            ValueError: If the query is blank or a bound is out of range.
        """

        text = required_text(query, field="query")
        moment = normalize_timestamp(now, field="now")
        segment_floor = non_negative(segment_threshold, field="segment_threshold")
        page_floor = fraction(page_threshold, field="page_threshold")
        depth = positive(top_k_segments, field="top_k_segments")
        terms = tokenize(text)
        query_keywords = keywords(text)
        if not terms:
            return []
        scored: list[tuple[float, Segment]] = []
        for block in self._candidates(terms):
            segment = self._read_segment(block, now=moment)
            score = self._match(segment.summary, segment.keyword_set, terms=terms, query_keywords=query_keywords)
            if score >= segment_floor:
                scored.append((score, segment))
        scored.sort(key=lambda entry: (-entry[0], entry[1].created_at, entry[1].segment_id))
        hits: list[PageHit] = []
        for score, segment in scored[:depth]:
            visited = self.visit(segment, now=moment)
            for page in visited.pages:
                page_score = overlap(terms, page.terms)
                if page_score >= page_floor and page_score > 0.0:
                    hits.append(
                        PageHit(
                            page=page,
                            segment_id=visited.segment_id,
                            segment_summary=visited.summary,
                            segment_score=score,
                            score=page_score,
                        )
                    )
        hits.sort(key=lambda hit: (-hit.score, -hit.segment_score, hit.page.occurred_at, hit.page.page_id))
        return hits

    def evict_coldest(self, *, exclude: str | None = None) -> Eviction | None:
        """Drop the least frequently used segment, with everything it owns.

        - This is upstream's LFU rule. The segment with the lowest access count
          goes, and the count is the one that promotion never resets.
        - The delete is a single subtree call, so the pages and their
          dialogue-chain edges go with the segment.
        - The id space has to be cleaned up by hand, because a binding names a
          block rather than copying it, and a name left pointing at a deleted
          block is worse than a name that is free.

        Args:
            exclude: Block id that must survive, normally the segment a batch
                was just written into.

        Returns:
            What was evicted, or `None` when nothing was eligible.
        """

        blocks = [block for block in self._segment_blocks() if block.id != exclude]
        if not blocks:
            return None
        coldest = min(blocks, key=lambda block: (int(block.value.get("access_count_lfu", 0)), block.value.get("timestamp", ""), block.value.get("session_id", "")))
        segment_id = str(coldest.value["session_id"])
        page_blocks = self._page_blocks(coldest)
        for page_block in page_blocks:
            self.structures.ids.unbind(
                MemoryAddress(PAGE_NAMESPACE, str(page_block.value["page_id"])),
                expected=page_block,
            )
        self.structures.ids.unbind(MemoryAddress(SEGMENT_NAMESPACE, segment_id), expected=coldest)
        self.structures.visits.remove(coldest)
        self.structures.segments.delete_subtree(coldest)
        return Eviction(
            tier="mid_term",
            reason="least frequently used segment",
            item_id=segment_id,
            detail=f"{coldest.value['summary']} ({len(page_blocks)} page(s))",
        )

    def chain_from(self, page_id: str, *, depth: int = 5) -> list[Page]:
        """Walk the dialogue chain forward from one page.

        The chain is the one relationship in MemoryOS that follows the
        conversation rather than the storage, so it is the one that crosses
        segments: two consecutive exchanges can be filed under different topics
        and still be linked.

        Args:
            page_id: Page to start from.
            depth: Maximum edges to follow.

        Returns:
            The pages reached, nearest first, without the starting page.

        Raises:
            ValueError: If the page id is blank or the depth is not positive.
        """

        start = self.page(page_id)
        if start is None:
            return []
        found: list[Page] = []
        seen = {start.id}
        frontier = start.block
        for _ in range(positive(depth, field="depth")):
            successors = self.structures.chain.successors(frontier)
            following = next((neighbor for neighbor in successors if neighbor.block.id not in seen), None)
            if following is None:
                break
            seen.add(following.block.id)
            found.append(_read_page(following.block))
            frontier = following.block
        return found

    def link_pages(self, previous: Page, current: Page) -> None:
        """Record that one page continues the conversation of another.

        Upstream keeps this as a `pre_page` and a `next_page` id on the two
        page dicts, writes both ends itself, and clears them when a page is
        dropped. One lineage edge replaces all of that:

        - it is a single write;
        - it is readable from either end;
        - it refuses an edge that would close a loop; and
        - it disappears when either page does.

        Args:
            previous: Page that came earlier.
            current: Page that continues it.
        """

        self.structures.chain.add(previous.block, current.block, metadata={"reason": "dialogue continuity"})

    def _best_match(self, summary: str, summary_keywords: frozenset[str], *, now: datetime) -> tuple[Segment | None, float]:
        """Return the open segment a batch should join, and how well it fits."""

        terms = tokenize(summary)
        scored = [
            (self._match(segment.summary, segment.keyword_set, terms=terms, query_keywords=summary_keywords), segment)
            for segment in self.segments(now=now)
        ]
        if not scored:
            return None, 0.0
        scored.sort(key=lambda entry: (-entry[0], entry[1].created_at, entry[1].segment_id))
        best_score, best = scored[0]
        if best_score < self.similarity_threshold:
            return None, best_score
        return best, best_score

    def _match(self, summary: str, segment_keywords: frozenset[str], *, terms: frozenset[str], query_keywords: frozenset[str]) -> float:
        """Score one segment the way upstream combines its two similarities."""

        return overlap(terms, tokenize(summary)) + self.keyword_alpha * jaccard(query_keywords, segment_keywords)

    def _open_segment(self, summary: str, summary_keywords: Sequence[str], *, now: datetime) -> MemoryBlock:
        """Create a segment block, bind its session id, and start its clock."""

        segment_id = new_id("session")
        block = self.structures.segments.create_child(
            type=SEGMENT_TYPE,
            value={
                "session_id": segment_id,
                "summary": summary,
                "summary_keywords": list(summary_keywords),
                "N_visit": 0,
                "access_count_lfu": 1,
                "timestamp": format_timestamp(now),
            },
            metadata={"tier": "mid_term", "session_id": segment_id},
        )
        self.structures.ids.bind(MemoryAddress(SEGMENT_NAMESPACE, segment_id), block)
        self.structures.visits.add(block, occurred_at=now)
        return block

    def _write_page(self, segment: MemoryBlock, exchange: Exchange) -> Page:
        """Create one page under a segment and bind its page id."""

        page_id = new_id("page")
        segment_id = str(segment.value["session_id"])
        block = self.structures.segments.create_child(
            parent=segment,
            type=PAGE_TYPE,
            value={"page_id": page_id, "analyzed": False, **exchange.as_json()},
            metadata={
                "tier": "mid_term",
                "page_id": page_id,
                "session_id": segment_id,
                "analyzed": False,
                "timestamp": format_timestamp(exchange.occurred_at),
            },
        )
        self.structures.ids.bind(MemoryAddress(PAGE_NAMESPACE, page_id), block)
        return _read_page(block)

    def _touch(self, segment: MemoryBlock, *, now: datetime) -> None:
        """Move a segment's last-visit time, which is what recency decays."""

        self.structures.visits.replace(segment, occurred_at=now)

    def _enforce_capacity(self, *, keep: str) -> tuple[Eviction, ...]:
        """Evict least-used segments until the tier is back inside capacity."""

        evicted: list[Eviction] = []
        while self.count() > self.capacity:
            eviction = self.evict_coldest(exclude=keep)
            if eviction is None:
                break
            evicted.append(eviction)
        return tuple(evicted)

    def _candidates(self, terms: frozenset[str]) -> list[MemoryBlock]:
        """Return the segments Genie's search offers for a set of query terms."""

        found: dict[str, MemoryBlock] = {}
        for term in sorted(terms):
            for result in self.memory.search(
                text=term,
                root=self.structures.mid_term_root,
                scope=Scope.SUBTREE,
                types=[SEGMENT_TYPE],
                limit=READ_LIMIT,
            ):
                found.setdefault(result.block.id, result.block)
        return [block for block in self._segment_blocks() if block.id in found]

    def _segment_blocks(self) -> list[MemoryBlock]:
        """Return the segment blocks in a stable creation order."""

        blocks = [block for block in self.structures.segments.children() if block.type == SEGMENT_TYPE]
        blocks.sort(key=lambda block: (str(block.value.get("timestamp", "")), str(block.value.get("session_id", ""))))
        return blocks

    def _page_blocks(self, segment: MemoryBlock) -> list[MemoryBlock]:
        """Return one segment's page blocks in dialogue order."""

        blocks = [block for block in self.structures.segments.children(segment) if block.type == PAGE_TYPE]
        blocks.sort(key=lambda block: (str(block.value.get("timestamp", "")), str(block.value.get("page_id", ""))))
        return blocks

    def _read_segment(self, block: MemoryBlock, *, now: datetime) -> Segment:
        """Assemble one segment record, computing the two derived heat terms."""

        item = self.structures.visits.get(block)
        created = parse_timestamp(block.value["timestamp"], field="timestamp")
        last_visit = item.entry.occurred_at if item is not None and item.entry.occurred_at is not None else created
        return Segment(
            block=block,
            segment_id=str(block.value["session_id"]),
            summary=str(block.value["summary"]),
            keywords=text_tuple(block.value.get("summary_keywords") or (), field="summary_keywords"),
            visits=int(block.value.get("N_visit", 0)),
            lfu_count=int(block.value.get("access_count_lfu", 0)),
            created_at=created,
            last_visit_at=last_visit,
            recency=time_decay(last_visit, now, tau_hours=self.tau_hours),
            pages=tuple(_read_page(page) for page in self._page_blocks(block)),
        )

    def _require_block(self, block_id: str) -> MemoryBlock:
        """Return a block that must still exist, or fail loudly."""

        block = self.memory.get(block_id)
        if block is None:
            raise LookupError(f"Mid-term block {block_id!r} disappeared during a write.")
        return block


def _read_page(block: MemoryBlock) -> Page:
    """Assemble one page record from its block."""

    return Page(
        block=block,
        page_id=str(block.value["page_id"]),
        segment_id=str(block.metadata.get("session_id", "")),
        exchange=exchange_from_json(block.value),
        analyzed=bool(block.value.get("analyzed", False)),
    )
