"""The slow tier of MemoryOS: the profile and the 2 knowledge bases.

Long-term memory is the only tier that is not a window. Data does not come here
because it arrived. It comes here because a mid-term segment became hot enough
for an analysis, and the analysis found something in it to keep after the
dialogue is gone.

MemoryOS keeps 3 things here:

- **Profile:** a description of the user.
- **User knowledge:** a base of facts about the user, with a capacity.
- **Assistant knowledge:** a separate base of what the assistant learned about
  how to do its job, with a capacity.

How they map to Genie:

- **Knowledge bases:** each one is an `EventLog` that the lamp keeps at a
  capacity. Upstream uses a deque with `maxlen`. A write past the capacity
  removes the oldest entry.
- **Profile:** a `VersionedRecords` identity. This is a deliberate difference.
  Upstream calls `update_user_profile(..., merge=False)`, so each analysis
  replaces the profile and the old profile is lost. Here the revision log keeps
  each old profile with no extra work. So the question "what did it think
  before this?" has an answer.
- **Provenance:** each fact keeps the pages that it came from, as ids in its own
  record and as `memoryos.extracted_from` edges. Upstream stores a knowledge
  entry as text, a timestamp, and an embedding. So upstream cannot tell where a
  fact came from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from examples.lamps.memoryos.model import (
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
from examples.lamps.memoryos.structures import MemoryOSStructures
from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import EventLog, MemoryAddress


class LongTermMemory:
    """The user profile, the knowledge base of the user, and the knowledge base of the assistant."""

    def __init__(self, memory: MemoryHandle, *, structures: MemoryOSStructures, knowledge_capacity: int = 100) -> None:
        """Bind the slow tier to the long-term stores of one user.

        Args:
            memory: Handle bounded to the memory of one user.
            structures: Structure bundle for that memory.
            knowledge_capacity: Maximum number of entries in each knowledge
                base, `long_term_knowledge_capacity` upstream.

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
        """Write the profile from an analysis as its next revision.

        - **Upstream:** the analysis receives the current profile and returns a
          full profile, which replaces the current profile.
        - **Here:** the method merges the new traits over the current traits,
          which has the same effect. It appends a revision and does not
          overwrite, so the old profile stays readable.

        Args:
            traits: Traits that the analysis set. An empty mapping writes
                nothing.
            now: Time when the analysis finished.
            source_page_ids: Ids of the pages that the analysis read.

        Returns:
            The new revision, or `None` when the analysis set no traits.
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
            The newest revision, or `None` when no analysis set traits yet.
        """

        item = self.structures.profile.current(PROFILE_ADDRESS)
        return None if item is None else _revision(item.block, item.entry.revision)

    def profile_history(self, *, limit: int = 100) -> list[ProfileRevision]:
        """Return each profile that an analysis wrote, oldest first.

        Args:
            limit: Maximum number of revisions to return.

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
        """Append one fact to a knowledge base, and remove the oldest fact if the base is over capacity.

        Args:
            owner: The owner of the knowledge base to write to.
            knowledge: The fact.
            now: Time when the analysis finished.
            source_page_ids: Ids of the pages that the fact came from.

        Returns:
            The stored entry, and the entry that the capacity rule removed or
            `None`.

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
            owner: The owner of the knowledge base to read.
            limit: Maximum number of entries to return.

        Returns:
            The entries in write order.
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

        - **Candidates:** Genie selects them inside the tier of that owner, with
          one search for each query term.
        - **Score:** the fraction of query terms that the fact contains. An
          entry with a score of zero is never kept.
        - **Upstream:** it uses cosine distance over embeddings, with the same
          thresholds.

        Args:
            owner: The owner of the knowledge base to search.
            query: Free text to retrieve entries for.
            threshold: Minimum score to keep an entry, as upstream.
            top_k: Maximum number of entries to return.

        Returns:
            Hits, highest score first. Ties break on write time and then on
            text, so all runs give the same result.

        Raises:
            ValueError: If the query is blank or a limit is out of range.
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
        """Return the pages that a stored fact or profile revision came from.

        - **2 sources:** the record keeps the ids of the pages that the analysis
          read. The graph keeps edges to the pages.
        - **They can disagree:** when mid-term memory evicts a page, its edges go
          away, but the ids stay in the record.
        - **The difference is useful:** the ids tell what the analysis read. The
          edges tell which pages a reader can still read.

        Args:
            block: A knowledge entry block or a profile revision block.

        Returns:
            The page blocks that the edges still reach, in page id order.
        """

        neighbors = self.structures.provenance.neighbors(block, relation=EXTRACTED_FROM, direction="out")
        pages = [neighbor.block for neighbor in neighbors]
        pages.sort(key=lambda page: str(page.value.get("page_id", "")))
        return pages

    def counts(self) -> dict[str, int]:
        """Return the quantity in each long-term store.

        Returns:
            The number of entries in each knowledge base, and the revision
            number of the profile.
        """

        current = self.profile()
        return {
            "user_profile_revision": current.revision if current is not None else 0,
            "user_knowledge": len(self.knowledge(USER)),
            "assistant_knowledge": len(self.knowledge(ASSISTANT)),
        }

    def _profile_identity(self) -> MemoryBlock:
        """Return the stable identity of the profile, and register it the first time."""

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
        """Link a promoted record to the pages that it came from. A page id with no page gets no edge."""

        for page_id in source_page_ids:
            page = self.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, page_id))
            if page is not None:
                self.structures.provenance.add_edge(block, page, EXTRACTED_FROM, metadata={"page_id": page_id})

    def _enforce_capacity(self, owner: KnowledgeOwner) -> Eviction | None:
        """Remove the oldest entry when a knowledge base is over capacity."""

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
        """Return the ids that the Genie search finds for a set of query terms."""

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
        """Return the `EventLog` of one knowledge base."""

        return self.structures.user_knowledge if owner == USER else self.structures.assistant_knowledge


def _knowledge(block: MemoryBlock, sequence: int) -> KnowledgeItem:
    """Build one knowledge entry from its block and its log position."""

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
    """Build one profile revision from its block and its revision number."""

    return ProfileRevision(
        block=block,
        revision=revision,
        data=string_mapping(block.value.get("profile"), field="profile"),
        recorded_at=parse_timestamp(block.value["timestamp"], field="timestamp"),
        source_page_ids=text_tuple(block.value.get("source_page_ids") or (), field="source_page_ids"),
    )
