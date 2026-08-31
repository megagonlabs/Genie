"""MemoryOS's slow tier: the profile and the two knowledge bases.

Long-term memory is the only tier that is not a window. Nothing lands here
because it arrived. It lands here because a mid-term segment got hot enough for
an analysis to read it, and the analysis decided that something in it was worth
keeping after the dialogue itself is gone.

MemoryOS keeps three things here:

- a profile of who the user is;
- a capped base of facts about them; and
- a separate capped base of what the assistant learned about doing its job.

How they map:

- The two knowledge bases are `EventLog`s with a capacity, which is the deque
  with `maxlen` upstream. Writing past the cap drops the oldest entry.
- The profile is a `VersionedRecords` identity instead, and that is a deliberate
  difference. Upstream calls `update_user_profile(..., merge=False)`, so each
  analysis overwrites the profile and the reading it replaced is gone. A
  revision log costs nothing extra here and keeps them, so "what did it think
  before this?" has an answer.
- Every fact carries the pages it was extracted from, both as ids in its own
  record and as `memoryos.extracted_from` edges. Upstream stores a knowledge
  entry as text, a timestamp, and an embedding, so it cannot answer where a fact
  came from at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import EventLog, MemoryAddress
from lamps.memoryos.model import (
    ASSISTANT,
    EXTRACTED_FROM,
    KNOWLEDGE_TYPE,
    PAGE_NAMESPACE,
    PROFILE_ADDRESS,
    PROFILE_TYPE,
    READ_LIMIT,
    REVISION_TYPE,
    USER,
    Eviction,
    KnowledgeHit,
    KnowledgeItem,
    KnowledgeOwner,
    ProfileRevision,
    format_timestamp,
    fraction,
    normalize_timestamp,
    overlap,
    parse_timestamp,
    positive,
    required_text,
    string_mapping,
    text_tuple,
    tokenize,
)
from lamps.memoryos.structures import MemoryOSStructures


class LongTermMemory:
    """The user profile, the user's knowledge base, and the assistant's."""

    def __init__(self, memory: MemoryHandle, *, structures: MemoryOSStructures, knowledge_capacity: int = 100) -> None:
        """Bind the slow tier to one user's long-term stores.

        Args:
            memory: Handle bounded to one user's memory.
            structures: Structure bundle for that memory.
            knowledge_capacity: Entries each knowledge base holds,
                `long_term_knowledge_capacity` upstream.

        Raises:
            TypeError: If the capacity is not an integer.
            ValueError: If the capacity is not positive.
        """

        self.memory = memory
        self.structures = structures
        self.knowledge_capacity = positive(knowledge_capacity, field="knowledge_capacity")

    def update_profile(
        self,
        traits: Mapping[str, str],
        *,
        now: datetime,
        source_page_ids: Sequence[str] = (),
    ) -> ProfileRevision | None:
        """Write the profile an analysis produced as its next revision.

        Upstream the analysis is handed the current profile and returns the whole
        thing, which then replaces it. Merging the new traits over the current
        ones is the same effect, and appending rather than overwriting is what
        keeps the reading it replaced readable.

        Args:
            traits: Traits the analysis set. An empty mapping writes nothing.
            now: Time the analysis finished.
            source_page_ids: Pages the analysis read.

        Returns:
            The new revision, or `None` when the analysis changed no traits.
        """

        changes = string_mapping(traits, field="traits")
        if not changes:
            return None
        moment = normalize_timestamp(now, field="now")
        sources = text_tuple(source_page_ids, field="source_page_ids")
        current = self.profile()
        merged = {**(current.data if current is not None else {}), **changes}
        self._profile_identity()
        block = self.structures.tiers.create_child(
            parent=self.structures.user_root,
            type=REVISION_TYPE,
            value={"profile": merged, "timestamp": format_timestamp(moment), "source_page_ids": list(sources)},
            metadata={"tier": "user_long_term", "kind": "profile_revision"},
        )
        item = self.structures.profile.append(PROFILE_ADDRESS, block)
        self._record_sources(block, sources)
        return _revision(item.block, item.entry.revision)

    def profile(self) -> ProfileRevision | None:
        """Return the current profile.

        Returns:
            The newest revision, or `None` when no analysis has run.
        """

        item = self.structures.profile.current(PROFILE_ADDRESS)
        return None if item is None else _revision(item.block, item.entry.revision)

    def profile_history(self, *, limit: int = 100) -> list[ProfileRevision]:
        """Return every profile an analysis has written, oldest first.

        Args:
            limit: Maximum revisions to return.

        Returns:
            The revisions in order.
        """

        return [_revision(item.block, item.entry.revision) for item in self.structures.profile.history(PROFILE_ADDRESS, limit=limit)]

    def add_knowledge(
        self,
        owner: KnowledgeOwner,
        knowledge: str,
        *,
        now: datetime,
        source_page_ids: Sequence[str] = (),
    ) -> tuple[KnowledgeItem, Eviction | None]:
        """Append one fact to a knowledge base, dropping the oldest if full.

        Args:
            owner: Which knowledge base to write to.
            knowledge: The fact.
            now: Time the analysis finished.
            source_page_ids: Pages the fact was extracted from.

        Returns:
            The stored entry and the entry the capacity rule dropped, if any.

        Raises:
            ValueError: If the fact is blank.
        """

        text = required_text(knowledge, field="knowledge")
        moment = normalize_timestamp(now, field="now")
        sources = text_tuple(source_page_ids, field="source_page_ids")
        block = self.structures.tiers.create_child(
            parent=self._root(owner),
            type=KNOWLEDGE_TYPE,
            value={
                "owner": owner,
                "knowledge": text,
                "timestamp": format_timestamp(moment),
                "source_page_ids": list(sources),
            },
            metadata={"tier": f"{owner}_long_term", "owner": owner},
        )
        item = self._log(owner).append(block)
        self._record_sources(block, sources)
        stored = KnowledgeItem(
            block=item.block,
            owner=owner,
            knowledge=text,
            recorded_at=moment,
            sequence=item.entry.sequence,
            source_page_ids=sources,
        )
        return stored, self._enforce_capacity(owner)

    def knowledge(self, owner: KnowledgeOwner, *, limit: int = READ_LIMIT) -> list[KnowledgeItem]:
        """Return one knowledge base, oldest first.

        Args:
            owner: Which knowledge base to read.
            limit: Maximum entries to return.

        Returns:
            The entries in the order they were written.
        """

        return [_knowledge(item.block, item.entry.sequence) for item in self._log(owner).read(limit=limit)]

    def search_knowledge(
        self,
        owner: KnowledgeOwner,
        query: str,
        *,
        threshold: float = 0.01,
        top_k: int = 20,
    ) -> list[KnowledgeHit]:
        """Return the entries in one knowledge base that match a query.

        Genie selects the candidates inside that owner's tier, one search per
        query term, and the score is the share of query terms the fact contains.
        Upstream this is cosine distance over embeddings against the same
        thresholds.

        Args:
            owner: Which knowledge base to search.
            query: Free text to retrieve for.
            threshold: Minimum score to keep, as upstream.
            top_k: Maximum entries to return.

        Returns:
            Hits in descending score order, ties broken on write time and then
            text so runs agree.

        Raises:
            ValueError: If the query is blank or a bound is out of range.
        """

        text = required_text(query, field="query")
        floor = fraction(threshold, field="threshold")
        depth = positive(top_k, field="top_k")
        terms = tokenize(text)
        if not terms:
            return []
        candidates = self._candidates(owner, terms)
        scored = [(item, overlap(terms, item.terms)) for item in self.knowledge(owner) if item.id in candidates]
        hits = [KnowledgeHit(item=item, score=score) for item, score in scored if score > 0.0 and score >= floor]
        hits.sort(key=lambda hit: (-hit.score, hit.item.recorded_at, hit.item.knowledge))
        return hits[:depth]

    def sources_of(self, block: MemoryBlock) -> list[MemoryBlock]:
        """Return the pages a stored fact or profile revision was taken from.

        - The record keeps the page ids it read, and the graph keeps edges to
          the pages themselves.
        - The two can disagree, because a page evicted from mid-term memory
          takes its edges with it while the ids stay written down.
        - That difference is the useful part. The ids say what the analysis
          read, and the edges say what can still be read back.

        Args:
            block: A knowledge entry or profile revision block.

        Returns:
            The page blocks still reachable, in id order.
        """

        neighbors = self.structures.provenance.neighbors(block, relation=EXTRACTED_FROM, direction="out")
        pages = [neighbor.block for neighbor in neighbors]
        pages.sort(key=lambda page: str(page.value.get("page_id", "")))
        return pages

    def counts(self) -> dict[str, int]:
        """Return how much each long-term store holds.

        Returns:
            Entry counts per store, plus the profile's revision number.
        """

        current = self.profile()
        return {
            "user_profile_revision": current.revision if current is not None else 0,
            "user_knowledge": len(self.knowledge(USER)),
            "assistant_knowledge": len(self.knowledge(ASSISTANT)),
        }

    def _profile_identity(self) -> MemoryBlock:
        """Return the profile's stable identity, registering it once."""

        existing = self.structures.profile.identity(PROFILE_ADDRESS)
        if existing is not None:
            return existing
        identity = self.structures.tiers.create_child(
            parent=self.structures.user_root,
            type=PROFILE_TYPE,
            value={"record": "user_profile"},
            metadata={"tier": "user_long_term", "kind": "profile"},
        )
        self.structures.profile.register(PROFILE_ADDRESS, identity)
        return identity

    def _record_sources(self, block: MemoryBlock, source_page_ids: Sequence[str]) -> None:
        """Link a promoted record to the pages it was extracted from."""

        for page_id in source_page_ids:
            page = self.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, page_id))
            if page is not None:
                self.structures.provenance.add_edge(block, page, EXTRACTED_FROM, metadata={"page_id": page_id})

    def _enforce_capacity(self, owner: KnowledgeOwner) -> Eviction | None:
        """Drop the oldest entry when a knowledge base is over capacity."""

        entries = self.knowledge(owner)
        if len(entries) <= self.knowledge_capacity:
            return None
        oldest = entries[0]
        self.memory.delete(oldest.block)
        return Eviction(
            tier=f"{owner}_long_term",
            reason="knowledge base at capacity",
            item_id=oldest.block.id,
            detail=oldest.knowledge,
        )

    def _candidates(self, owner: KnowledgeOwner, terms: frozenset[str]) -> set[str]:
        """Return the ids Genie's search offers for a set of query terms."""

        found: set[str] = set()
        for term in sorted(terms):
            for result in self.memory.search(
                text=term,
                root=self._root(owner),
                scope=Scope.SUBTREE,
                types=[KNOWLEDGE_TYPE],
                limit=READ_LIMIT,
            ):
                found.add(result.block.id)
        return found

    def _root(self, owner: KnowledgeOwner) -> MemoryBlock:
        """Return the tier anchor for one knowledge base."""

        return self.structures.user_root if owner == USER else self.structures.assistant_root

    def _log(self, owner: KnowledgeOwner) -> EventLog:
        """Return the capped log for one knowledge base."""

        return self.structures.user_knowledge if owner == USER else self.structures.assistant_knowledge


def _knowledge(block: MemoryBlock, sequence: int) -> KnowledgeItem:
    """Assemble one knowledge entry from its block and log position."""

    owner = USER if block.value.get("owner") == USER else ASSISTANT
    return KnowledgeItem(
        block=block,
        owner=owner,
        knowledge=str(block.value["knowledge"]),
        recorded_at=parse_timestamp(block.value["timestamp"], field="timestamp"),
        sequence=sequence,
        source_page_ids=text_tuple(block.value.get("source_page_ids") or (), field="source_page_ids"),
    )


def _revision(block: MemoryBlock, revision: int) -> ProfileRevision:
    """Assemble one profile revision from its block and revision number."""

    return ProfileRevision(
        block=block,
        revision=revision,
        data=string_mapping(block.value.get("profile"), field="profile"),
        recorded_at=parse_timestamp(block.value["timestamp"], field="timestamp"),
        source_page_ids=text_tuple(block.value.get("source_page_ids") or (), field="source_page_ids"),
    )
