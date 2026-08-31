"""The store Mem0's memories live in, addressed by id and scoped by session.

Upstream this is a vector collection:

- Each memory is one payload holding the text, its digest, and the session
  identifiers.
- `get`, `update`, and `delete` take a memory id.
- `get_all` and `search` take the identifiers and turn them into a filter.

This module is that surface, with the two lookups split across the two Genie
structures that fit them: the id is a `KeyedCollection` address, and the session
is a path in the ownership tree.

- The split matters most for the operations a filter cannot express. Each path
  segment owns the memories addressed below it, so `drop` retires a user, an
  agent, or a single run with one call, and a narrower session is simply a
  deeper search root.
- Retrieval is deliberately two different things here, as it is upstream. A
  search takes a query and returns what matches its terms. The neighbor lookup
  the update phase makes takes a whole candidate fact and returns the stored
  memories closest to it.
- Genie selects candidates in both cases - the scope, the block type, and any
  metadata filter are the query - and the lamp scores them, because Genie
  supplies no ranking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import MemoryAddress
from lamps.mem0.model import (
    FACTUAL,
    MEMORY_NAMESPACE,
    MEMORY_TYPE,
    READ_LIMIT,
    SCOPE_TYPE,
    SearchHit,
    Session,
    StoredMemory,
    content_hash,
    format_timestamp,
    json_mapping,
    memory_from_block,
    memory_metadata,
    memory_value,
    new_memory_id,
    normalize_timestamp,
    overlap,
    required_text,
    tokenize,
)
from lamps.mem0.structures import Mem0Structures


class MemoryStore:
    """Scoped memory records addressed by a stable Mem0 memory id.

    The store owns placement, addressing, and retrieval. It never decides
    whether a new fact should add, replace, or remove a record; the pipeline
    does that.
    """

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create a store over one bounded handle.

        Args:
            memory: Bounded Genie handle owning every session path and memory.
            structures: Facade bundle built over the same handle.
        """

        self.memory = memory
        self.structures = structures

    def create(
        self,
        session: Session,
        text: str,
        *,
        kind: str = FACTUAL,
        metadata: Mapping[str, Any] | None = None,
        at: datetime,
    ) -> StoredMemory:
        """Store a new memory in one session's scope.

        Args:
            session: Scope the memory belongs to. Its path is created when this
                is the first memory stored under it.
            text: Memory text.
            kind: `factual` or `procedural_memory`.
            metadata: Application metadata to keep and index with the record.
            at: Time recorded as both creation and update time.

        Returns:
            The stored memory.

        Raises:
            ValueError: If the text is empty or the kind is unknown.
        """

        written_at = normalize_timestamp(at, field="at")
        memory_text = required_text(text, field="memory")
        memory_id = new_memory_id()
        supplied = json_mapping(metadata, field="metadata")
        block = self.structures.tiers.create_child(
            parent=self.ensure_scope(session),
            type=MEMORY_TYPE,
            value=memory_value(
                session,
                memory_id,
                memory_text,
                kind=kind,
                metadata=supplied,
                created_at=written_at,
                updated_at=written_at,
            ),
            metadata=memory_metadata(session, memory_id, memory_text, kind=kind, metadata=supplied),
        )
        try:
            self.structures.records.bind(MemoryAddress(MEMORY_NAMESPACE, memory_id), block)
        except Exception:
            self.memory.delete(block)
            raise
        return memory_from_block(block)

    def replace(
        self,
        memory_id: str,
        text: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        at: datetime,
    ) -> StoredMemory:
        """Rewrite one memory in place, keeping its id and creation time.

        Mem0's update phase patches the stored record rather than adding beside
        it, so callers holding the id keep a valid reference. A Genie update
        keeps the block id and raises `StoredMemory.version`, which makes the
        patch visible without the lamp counting writes itself.

        Args:
            memory_id: Id of the memory to rewrite.
            text: Replacement text.
            metadata: Application metadata to merge into the record.
            at: Time recorded as the new update time.

        Returns:
            The memory after the write, with the same id as before.

        Raises:
            KeyError: If no memory has that id.
            ValueError: If the text is empty.
        """

        existing = self.require(memory_id)
        written_at = normalize_timestamp(at, field="at")
        memory_text = required_text(text, field="memory")
        merged = {**existing.metadata, **json_mapping(metadata, field="metadata")}
        block = self.memory.update(
            existing.block,
            value={
                "memory": memory_text,
                "hash": content_hash(memory_text),
                "metadata": merged,
                "updated_at": format_timestamp(written_at),
            },
            metadata=memory_metadata(
                existing.session,
                existing.memory_id,
                memory_text,
                kind=existing.kind,
                metadata=merged,
            ),
        )
        return memory_from_block(block)

    def remove(self, memory_id: str) -> StoredMemory:
        """Delete one memory and return its last stored state.

        The keyed binding goes with the block it pointed at, so the id is freed
        rather than left dangling. The memory's recorded history is untouched,
        because those events are owned by the history tier.

        Args:
            memory_id: Id of the memory to delete.

        Returns:
            The memory as it was before deletion.

        Raises:
            KeyError: If no memory has that id.
        """

        existing = self.require(memory_id)
        self.memory.delete(existing.block)
        return existing

    def get(self, memory_id: str) -> StoredMemory | None:
        """Look one memory up by its id, without naming a scope.

        Args:
            memory_id: Id to look up.

        Returns:
            The memory, or `None` when the id is unknown.
        """

        address = MemoryAddress(MEMORY_NAMESPACE, required_text(memory_id, field="memory_id"))
        block = self.structures.records.get(address)
        return None if block is None else memory_from_block(block)

    def require(self, memory_id: str) -> StoredMemory:
        """Look one memory up and fail when the id is unknown.

        Args:
            memory_id: Id to look up.

        Returns:
            The stored memory.

        Raises:
            KeyError: If no memory has that id.
        """

        memory = self.get(memory_id)
        if memory is None:
            raise KeyError(f"Memory {memory_id!r} was not found.")
        return memory

    def memories(
        self,
        session: Session,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = READ_LIMIT,
    ) -> list[StoredMemory]:
        """Return every memory in one session's scope.

        A session names a path, so a scope is a search root: asking for
        `user:alice` returns the memories of every agent and run below her,
        which is what filtering on `user_id` alone does upstream.

        Args:
            session: Scope to read.
            filters: Metadata filter passed through to Genie.
            limit: Maximum memories to return.

        Returns:
            Memories in ascending creation-time and text order.
        """

        return self._sorted(self._candidates(session, filters=filters, limit=limit).values())

    def search(
        self,
        session: Session,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        threshold: float = 0.0,
    ) -> list[SearchHit]:
        """Return the memories in scope that match a query's terms.

        - Genie selects the candidates. The session path becomes a subtree
          scope, the block type and any metadata filter go into the query, and
          one search runs per query term.
        - Scoring is the lamp's, because Genie's portable text match is a
          substring test rather than a similarity measure. A candidate counts a
          term only when it appears in the memory's own text, and the score is
          the share of terms that do.

        Args:
            session: Scope to search below.
            query: Free text whose terms are matched against memory text.
            filters: Metadata filter passed through to Genie, such as
                `{"memory_type": "procedural_memory"}`.
            limit: Maximum hits to return.
            threshold: Minimum score a hit must reach, as upstream.

        Returns:
            Hits in descending score order, ties broken on creation time and
            then text so repeated runs agree.

        Raises:
            ValueError: If the query is empty, the limit is not positive, or
                the threshold is outside zero to one.
        """

        terms = tokenize(required_text(query, field="query"))
        page = _positive(limit, field="limit")
        floor = _fraction(threshold, field="threshold")
        node = self.scope_node(session)
        if node is None or not terms:
            return []
        candidates: dict[str, MemoryBlock] = {}
        for term in terms:
            for result in self.memory.search(
                text=term,
                root=node,
                scope=Scope.SUBTREE,
                types=[MEMORY_TYPE],
                filters=dict(filters) if filters else None,
                limit=READ_LIMIT,
            ):
                candidates.setdefault(result.block.id, result.block)
        hits: list[SearchHit] = []
        for memory in self._sorted(candidates.values()):
            present = memory.terms
            matched = tuple(term for term in terms if term in present)
            score = round(len(matched) / len(terms), 4)
            if matched and score >= floor:
                hits.append(SearchHit(memory=memory, score=score, matched=matched))
        hits.sort(key=lambda hit: (-hit.score, hit.memory.created_at, hit.memory.text))
        return hits[:page]

    def neighbors(self, session: Session, text: str, *, limit: int = 5) -> list[tuple[StoredMemory, float]]:
        """Return the stored memories closest to one candidate fact.

        This is the read Mem0 makes between its two phases: before deciding
        what to do with an extracted fact, it fetches the most similar memories
        already in scope so the decision is made against what is on file. The
        comparison is over whole records rather than query terms, which is why
        it is a separate operation from `search`.

        Args:
            session: Scope to compare within.
            text: Candidate fact.
            limit: Maximum neighbors to return.

        Returns:
            Memories paired with their similarity, most similar first, ties
            broken on creation time and then text.

        Raises:
            ValueError: If the text is empty or the limit is not positive.
        """

        terms = tokenize(required_text(text, field="text"))
        page = _positive(limit, field="limit")
        scored = [(memory, overlap(terms, memory.terms)) for memory in self.memories(session)]
        ranked = [entry for entry in scored if entry[1] > 0.0]
        ranked.sort(key=lambda entry: (-entry[1], entry[0].created_at, entry[0].text))
        return ranked[:page]

    def drop(self, session: Session) -> list[StoredMemory]:
        """Delete one session's scope and every memory below it.

        A payload filter has no answer for "this user is gone"; every matching
        record has to be found and deleted one at a time. Because each path
        segment owns what is stored below it, ownership answers it in one call,
        and the keyed bindings go with the blocks they pointed at.

        Args:
            session: Scope to retire, at whatever level its identifiers name.

        Returns:
            The memories that were removed, in ascending creation-time order.
        """

        node = self.scope_node(session)
        if node is None:
            return []
        removed = self.memories(session)
        self.structures.tiers.delete_subtree(node)
        return removed

    def scope_node(self, session: Session) -> MemoryBlock | None:
        """Return the block owning one session's scope, when it exists.

        Args:
            session: Scope to look up.

        Returns:
            The deepest path block, or `None` when nothing has been stored
            under that session yet.
        """

        node = self.structures.memory_root
        for segment in session.path:
            child = self._segment(node, segment)
            if child is None:
                return None
            node = child
        return node

    def ensure_scope(self, session: Session) -> MemoryBlock:
        """Return the block owning one session's scope, creating the path.

        Args:
            session: Scope to place a memory in.

        Returns:
            The deepest path block, after creating any missing segment.
        """

        node = self.structures.memory_root
        for depth, segment in enumerate(session.path, start=1):
            existing = self._segment(node, segment)
            if existing is not None:
                node = existing
                continue
            node = self.structures.tiers.create_child(
                parent=node,
                type=SCOPE_TYPE,
                value={"segment": segment, "path": list(session.path[:depth])},
                metadata={"segment": segment, "scope": "/".join(session.path[:depth])},
            )
        return node

    def _candidates(
        self,
        session: Session,
        *,
        filters: Mapping[str, Any] | None,
        limit: int,
    ) -> dict[str, MemoryBlock]:
        """Return the memory blocks Genie selects for one scope."""

        node = self.scope_node(session)
        if node is None:
            return {}
        return {
            result.block.id: result.block
            for result in self.memory.search(
                root=node,
                scope=Scope.SUBTREE,
                types=[MEMORY_TYPE],
                filters=dict(filters) if filters else None,
                limit=_positive(limit, field="limit"),
            )
        }

    def _segment(self, parent: MemoryBlock, segment: str) -> MemoryBlock | None:
        """Return the path child with this segment, when it exists."""

        return next(
            (
                child
                for child in self.memory.children(parent)
                if child.type == SCOPE_TYPE and child.metadata.get("segment") == segment
            ),
            None,
        )

    def _sorted(self, blocks: Iterable[MemoryBlock]) -> list[StoredMemory]:
        """Decode blocks into memories in a stable, run-independent order."""

        memories = [memory_from_block(block) for block in blocks]
        return sorted(memories, key=lambda memory: (memory.created_at, memory.text))


def _positive(value: object, *, field: str) -> int:
    """Return a positive integer bound.

    Args:
        value: Candidate bound.
        field: Field name used in error messages.

    Returns:
        The validated integer.

    Raises:
        TypeError: If the value is not an integer.
        ValueError: If the value is not positive.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer.")
    if value < 1:
        raise ValueError(f"{field} must be greater than zero.")
    return value


def _fraction(value: object, *, field: str) -> float:
    """Return a score bound between zero and one.

    Args:
        value: Candidate bound.
        field: Field name used in error messages.

    Returns:
        The validated float.

    Raises:
        TypeError: If the value is not a number.
        ValueError: If the value is outside zero to one.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} must be between zero and one.")
    return float(value)
