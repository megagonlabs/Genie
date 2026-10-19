"""The paged tier of MemoryOS: topic segments, their pages, and their heat.

In mid-term memory, MemoryOS changes from a transcript into a memory system.
The lamp does not file a page from short-term memory by time. It files the page
with the other pages about the same subject, in a *session*. MemoryOS also
calls a session a segment. Each segment has a score:

    H_segment = alpha * N_visit + beta * L_interaction + gamma * R_recency

This score is the full promotion policy:

- **`N_visit`:** retrieval increases it.
- **`L_interaction`:** more paged-in dialogue increases it.
- **`R_recency`:** time decreases it.
- **Hot segment:** a segment that comes up again and again becomes hot enough
  for an analysis into long-term memory.
- **Cold segment:** a segment that never comes up becomes cold. Its access count
  stays low, so eviction removes it at some time when the tier has no more
  room.

The lamp reads 2 of the 3 terms from the store. It does not keep them in step
with the store by hand:

- **`L_interaction`:** the number of pages of the segment that no analysis has
  read. The upstream counter holds the same number, because upstream sets it to
  zero at the same time that it marks each page as analyzed.
- **`R_recency`:** decays the last-visit time in a `TemporalCollection`.
- **No drift:** heat always agrees with the pages that it describes. No cached
  number can become stale. Heat is only a formula over the stored data.

The pages are the children of the segment. Ownership gives this result:

- **Eviction is one call:** one `delete_subtree` removes the segment, its pages,
  and their dialogue-chain edges.
- **Upstream:** it walks the pages that it will drop, and it clears the
  `pre_page`/`next_page` fields that refer to them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from examples.lamps.memoryos.model import (
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
from examples.lamps.memoryos.structures import MemoryOSStructures
from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import MemoryAddress


@dataclass(frozen=True, slots=True)
class SegmentWrite:
    """What one batch of paged-out exchanges did to mid-term memory.

    Attributes:
        segment: The segment that received the pages.
        pages: The pages that the batch wrote, in batch order.
        opened: `True` if the lamp opened a new segment for them.
        score: How well the batch matched the segment that it joined. When the
            lamp opened a new segment, this is the best score of the segments
            that were open, which was below the threshold. It is zero when no
            segment was open.
        evictions: Segments that the capacity rule removed to make room.
    """

    segment: Segment
    pages: tuple[Page, ...]
    opened: bool
    score: float
    evictions: tuple[Eviction, ...]


class MidTermMemory:
    """Topic segments, the pages that they own, and the heat that ranks them."""

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
        """Bind the paged tier to the mid-term store of one user.

        Args:
            memory: Handle bounded to the memory of one user.
            structures: Structure bundle for that memory.
            capacity: Maximum number of segments in the tier,
                `mid_term_capacity` upstream.
            similarity_threshold: Minimum match score for a batch to join a
                segment. Below it, the batch opens a new segment.
                `mid_term_similarity_threshold` upstream.
            keyword_alpha: Weight on the keyword term of a match,
                `keyword_similarity_alpha` upstream.
            tau_hours: Recency time constant of the heat formula: the number of
                hours in which the recency factor decreases by a factor of e.
                Zero selects `RECENCY_TAU_HOURS`.

        Raises:
            TypeError: If a limit has the wrong runtime type.
            ValueError: If a limit is out of range.
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
        """File a batch of paged-out exchanges under the segment that matches it best.

        This is upstream `insert_pages_into_session`:

        - **Compare:** the batch comes with the themed summary that a model wrote
          for it. The method compares that summary with each open segment.
        - **Join:** the best match at or above the threshold receives the pages.
        - **Open:** if no segment is close enough, the method opens a segment.
          This is upstream `add_session`. Only a new segment can cause an
          eviction under the capacity rule.

        Args:
            exchanges: Exchanges that left short-term memory.
            summary: Themed summary of the batch.
            summary_keywords: Keywords that came with the summary. If there are
                none, the method uses the content words of the summary.
            now: Time of the write.

        Returns:
            The segment, the pages written into it, and the segments that the
            capacity rule evicted to make room.

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
        """Record that retrieval reached this segment, and make it hotter.

        - **A read is a write in MemoryOS:** when retrieval reaches a segment,
          this method adds one to `N_visit` and to the eviction counter
          `access_count_lfu`.
        - **Recency:** it also moves the last-visit time. The recency term
          decays from this time.
        - **Result:** retrieval decides what goes to long-term memory later.

        Args:
            segment: Segment that matched a query.
            now: Time of the retrieval.

        Returns:
            The segment as it is now, with its new heat.
        """

        moment = normalize_timestamp(now, field="now")
        self.memory.update(
            segment.block,
            value={"N_visit": segment.visits + 1, "access_count_lfu": segment.lfu_count + 1},
        )
        self._touch(segment.block, now=moment)
        return self._read_segment(self._require_block(segment.id), now=moment)

    def mark_analyzed(self, pages: Sequence[Page]) -> tuple[Page, ...]:
        """Mark pages as read by an analysis, so they do not count toward length.

        Args:
            pages: Pages that the analysis read.

        Returns:
            The pages as they are now.
        """

        marked: list[Page] = []
        for page in pages:
            block = self.memory.update(page.block, value={"analyzed": True}, metadata={"analyzed": True})
            marked.append(_read_page(block))
        return tuple(marked)

    def cool(self, segment: Segment, *, now: datetime) -> Segment:
        """Reset the visit term of a segment after a promotion of its pages.

        - **Upstream:** it sets `N_visit` and `L_interaction` to zero, moves the
          last-visit time to now, and calculates heat again.
        - **Here:** the method writes only the visit count, and it moves the
          last-visit time. Length decreases with no write, because the pages
          that the analysis marked do not count toward it.

        Args:
            segment: Segment that the analysis read.
            now: Time when the analysis finished.

        Returns:
            The cooled segment.
        """

        moment = normalize_timestamp(now, field="now")
        self.memory.update(segment.block, value={"N_visit": 0})
        self._touch(segment.block, now=moment)
        return self._read_segment(self._require_block(segment.id), now=moment)

    def segments(self, *, now: datetime) -> list[Segment]:
        """Return each open segment, oldest first.

        Args:
            now: Time for the heat calculation.

        Returns:
            The segments in creation order.
        """

        moment = normalize_timestamp(now, field="now")
        return [self._read_segment(block, now=moment) for block in self._segment_blocks()]

    def count(self) -> int:
        """Return the number of open segments.

        Returns:
            The number of segments in the tier.
        """

        return len(self._segment_blocks())

    def page_count(self) -> int:
        """Return the number of pages in all segments of the tier.

        Returns:
            The number of pages, analyzed or not.
        """

        return sum(len(self._page_blocks(block)) for block in self._segment_blocks())

    def segment(self, segment_id: str, *, now: datetime) -> Segment | None:
        """Return one segment by its session id.

        Args:
            segment_id: MemoryOS session id.
            now: Time for the heat calculation.

        Returns:
            The segment, or `None` when no session has that id.
        """

        block = self.structures.ids.get(MemoryAddress(SEGMENT_NAMESPACE, required_text(segment_id, field="segment_id")))
        if block is None:
            return None
        return self._read_segment(block, now=normalize_timestamp(now, field="now"))

    def page(self, page_id: str) -> Page | None:
        """Return one page by its page id.

        - **Upstream:** it walks the page list of each session until it finds a
          match.
        - **Here:** a page id is an address. So the method does one read, and
          the read does not become slower when the tier fills.

        Args:
            page_id: MemoryOS page id.

        Returns:
            The page, or `None` when no page has that id.
        """

        block = self.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, required_text(page_id, field="page_id")))
        return None if block is None else _read_page(block)

    def hottest(self, *, now: datetime) -> Segment | None:
        """Return the segment that the promotion check examines.

        Args:
            now: Time for the heat calculation.

        Returns:
            The hottest segment, or `None` when the tier is empty. If 2
            segments have the same heat, the older segment wins, and then the
            session id decides. So all runs give the same result.
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
        """Return the pages that match a query, and visit the segments that match.

        - **Candidates come from Genie:** the mid-term tier is a subtree scope,
          the block type limits the search to segments, and the method runs one
          search for each query term.
        - **Scores come from the lamp,** because Genie has no similarity
          measure. A segment score is the fraction of query terms in its
          summary, plus the MemoryOS keyword Jaccard term multiplied by
          `keyword_alpha`.
        - **Page score:** the fraction of query terms in the user input, the
          assistant reply, and the topic of a page. The method scores only the
          pages of the segments that it visits. A page with a score of zero is
          never kept.
        - **Visits:** the method visits only the `top_k_segments` best segments
          at or above `segment_threshold`.

        Args:
            query: Free text to retrieve pages for.
            now: Time of the retrieval. The method records it as a visit.
            segment_threshold: Minimum segment score for a search of its pages.
            page_threshold: Minimum page score to keep a page.
            top_k_segments: Maximum number of matched segments to search for
                pages.

        Returns:
            Page hits, highest score first. Ties break on the segment score
            (higher first), then on the page time, and then on the page id.

        Raises:
            ValueError: If the query is blank or a limit is out of range.
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
        """Remove the least frequently used segment, with all that it owns.

        - **Upstream LFU rule:** the segment with the lowest access count
          leaves. Promotion never resets this count. Ties break on the creation
          time (older first), and then on the session id.
        - **One subtree delete:** the pages and their dialogue-chain edges leave
          with the segment.
        - **Manual cleanup of the id space:** a binding refers to a block and
          does not copy it. A binding to a deleted block makes
          `KeyedCollection.get` raise `MemoryNotFound`, but a free name does
          not. So the method unbinds the page ids and the session id before
          the delete. It also removes the segment from the
          `TemporalCollection`.

        Args:
            exclude: Block id of a segment that this call does not evict,
                usually the segment that a batch just went into.

        Returns:
            What the method evicted, or `None` when no segment could be
            evicted.
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

        The chain is the only relationship in MemoryOS that follows the
        conversation and not the storage. So it can cross segments: the lamp can
        file 2 consecutive exchanges under different topics, and the chain still
        links them.

        Args:
            page_id: Page to start from.
            depth: Maximum number of edges to follow.

        Returns:
            The pages that the walk reached, nearest first, without the start
            page. The list is empty when no page has this id.

        Raises:
            ValueError: If the page id is blank, or if the page exists and the
                depth is not positive.
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
        """Record that one page continues the conversation of another page.

        Upstream keeps a `pre_page` id and a `next_page` id on the 2 page dicts.
        It writes both ends, and it clears them when it drops a page. One
        lineage edge replaces all of that:

        - **One write:** the lamp adds the edge with a single call.
        - **Both ends:** a reader can read the edge from either page.
        - **No loops:** the `LineageDAG` refuses an edge that would close a
          loop.
        - **Automatic removal:** Genie removes the edge when the lamp deletes
          either page.

        Args:
            previous: The earlier page.
            current: The page that continues it.
        """

        self.structures.chain.add(previous.block, current.block, metadata={"reason": "dialogue continuity"})

    def _best_match(self, summary: str, summary_keywords: frozenset[str], *, now: datetime) -> tuple[Segment | None, float]:
        """Return the open segment that a batch joins, or `None`, and the best match score."""

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
        """Score one segment as upstream combines its 2 similarity measures."""

        return overlap(terms, tokenize(summary)) + self.keyword_alpha * jaccard(query_keywords, segment_keywords)

    def _open_segment(self, summary: str, summary_keywords: Sequence[str], *, now: datetime) -> MemoryBlock:
        """Create a segment block, bind its session id, and record its first visit time."""

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
        """Move the last-visit time of a segment. The recency term decays from this time."""

        self.structures.visits.replace(segment, occurred_at=now)

    def _enforce_capacity(self, *, keep: str) -> tuple[Eviction, ...]:
        """Evict the least used segments until the tier is inside its capacity again."""

        evicted: list[Eviction] = []
        while self.count() > self.capacity:
            eviction = self.evict_coldest(exclude=keep)
            if eviction is None:
                break
            evicted.append(eviction)
        return tuple(evicted)

    def _candidates(self, terms: frozenset[str]) -> list[MemoryBlock]:
        """Return the segments that the Genie search finds for a set of query terms."""

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
        """Return the page blocks of one segment in dialogue order."""

        blocks = [block for block in self.structures.segments.children(segment) if block.type == PAGE_TYPE]
        blocks.sort(key=lambda block: (str(block.value.get("timestamp", "")), str(block.value.get("page_id", ""))))
        return blocks

    def _read_segment(self, block: MemoryBlock, *, now: datetime) -> Segment:
        """Build one segment record, and calculate the 2 derived heat terms."""

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
        """Return the block, or raise `LookupError` if the block no longer exists."""

        block = self.memory.get(block_id)
        if block is None:
            raise LookupError(f"Mid-term block {block_id!r} disappeared during a write.")
        return block


def _read_page(block: MemoryBlock) -> Page:
    """Build one page record from its block."""

    return Page(
        block=block,
        page_id=str(block.value["page_id"]),
        segment_id=str(block.metadata.get("session_id", "")),
        exchange=exchange_from_json(block.value),
        analyzed=bool(block.value.get("analyzed", False)),
    )
