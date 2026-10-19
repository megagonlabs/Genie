"""The store of Mem0 memories, with an id address and a session scope for each memory.

Upstream, this store is a vector collection:

- **Payload:** each memory is one payload that holds the text, its digest, and
  the session identifiers.
- **Id calls:** `get`, `update`, and `delete` take a memory id.
- **Scope calls:** `get_all` and `search` take the identifiers and make a filter
  from them.

This module has the same calls. It puts the 2 lookups on the 2 Genie structures
that suit them:

- **Id:** the id is a `KeyedCollection` address.
- **Session:** the session is a path in the ownership tree.
- **Why the split matters:** it gives the operations that a filter cannot
  express. Each path segment owns the memories that have addresses below it.
  Thus `drop` retires a user, an agent, or a single run with one call. A
  narrower session is only a deeper search root.
- **2 kinds of retrieval:** as upstream, retrieval has 2 different forms here,
  on purpose. A search takes a query and returns the memories that match its
  terms. The neighbor lookup of the update phase takes a full candidate fact
  and returns the stored memories that are nearest to it.
- **Who does what:** in both forms, Genie selects the candidates. The scope, the
  block type, and any metadata filter are the query. The lamp calculates the
  scores, because Genie supplies no ranking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from examples.lamps.mem0.model import (
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
from examples.lamps.mem0.structures import Mem0Structures
from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import MemoryAddress


class MemoryStore:
    """Scoped memory records, each with a stable Mem0 memory id as its address.

    The store controls placement, addresses, and retrieval. It never decides if
    a new fact adds, replaces, or removes a record. The pipeline makes that
    decision.
    """

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create a store over one bounded handle.

        Args:
            memory: Bounded Genie handle that owns all session paths and
                memories.
            structures: Facade bundle over the same handle.
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
        """Store a new memory in the scope of one session.

        Args:
            session: Scope of the memory. If this is the first memory in the
                scope, the method creates the scope path.
            text: Memory text.
            kind: `factual` or `procedural_memory`.
            metadata: Application metadata to keep and index with the record.
            at: Time that the method records as the creation time and the
                update time.

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
        """Rewrite one memory in place, and keep its id and creation time.

        - **Upstream:** the Mem0 update phase changes the stored record. It
          does not add a new record beside it. Thus callers that hold the id
          keep a valid reference.
        - **Here:** a Genie update keeps the block id and increases
          `StoredMemory.version`. Thus the change is visible, and the lamp does
          not count the writes itself.

        Args:
            memory_id: Id of the memory to rewrite.
            text: New text.
            metadata: Application metadata to merge into the record.
            at: Time that the method records as the new update time.

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

        - **Binding:** Genie removes the keyed binding together with its block.
          Thus the id becomes free and does not refer to a deleted block.
        - **History:** the recorded history of the memory does not change,
          because the history tier owns those events.

        Args:
            memory_id: Id of the memory to delete.

        Returns:
            The memory as it was before the delete.

        Raises:
            KeyError: If no memory has that id.
        """

        existing = self.require(memory_id)
        self.memory.delete(existing.block)
        return existing

    def get(self, memory_id: str) -> StoredMemory | None:
        """Find one memory by its id, with no scope.

        Args:
            memory_id: Id to find.

        Returns:
            The memory, or `None` if the id is unknown.
        """

        address = MemoryAddress(MEMORY_NAMESPACE, required_text(memory_id, field="memory_id"))
        block = self.structures.records.get(address)
        return None if block is None else memory_from_block(block)

    def require(self, memory_id: str) -> StoredMemory:
        """Find one memory, and raise `KeyError` if the id is unknown.

        Args:
            memory_id: Id to find.

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
        """Return all memories in the scope of one session.

        A session names a path, so a scope is a search root. A read for
        `user:alice` returns the memories of all agents and runs below her.
        Upstream, a filter on only `user_id` gives the same result.

        Args:
            session: Scope to read.
            filters: Metadata filter for the Genie search.
            limit: Maximum number of memories to return.

        Returns:
            Memories in ascending order of creation time, and then of text.
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
        """Return the memories in scope that match the terms of a query.

        - **Genie selects the candidates:** the session path becomes a subtree
          scope. The block type and any metadata filter go into the query. One
          search runs for each query term.
        - **The lamp calculates the scores:** the portable text match of Genie
          is a substring test, not a similarity measure. A candidate counts a
          term only if the term occurs in the text of the memory. The score is
          the fraction of the terms that occur.

        Args:
            session: Scope to search below.
            query: Free text. The method matches its terms against the memory
                text.
            filters: Metadata filter for the Genie search, for example
                `{"memory_type": "procedural_memory"}`.
            limit: Maximum number of hits to return.
            threshold: Minimum score of a hit, as upstream.

        Returns:
            Hits in descending score order. For equal scores, the order uses
            creation time and then text, so 2 runs give the same order.

        Raises:
            TypeError: If the limit is not an integer, or the threshold is not
                a number.
            ValueError: If the query is empty, the limit is not positive, or
                the threshold is not from zero to one.
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
        """Return the stored memories that are nearest to one candidate fact.

        - **Upstream:** Mem0 does this read between its 2 phases. Before it
          decides about an extracted fact, it gets the most similar memories in
          scope. Thus the decision compares the fact with the stored memories.
        - **Not `search`:** the comparison uses full records, not query terms.
          Thus it is a separate operation.

        Args:
            session: Scope for the comparison.
            text: Candidate fact.
            limit: Maximum number of neighbors to return.

        Returns:
            Memories with their similarity, the most similar first. For equal
            similarity, the order uses creation time and then text. Memories
            with zero similarity are not in the result.

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
        """Delete the scope of one session and all memories below it.

        - **Without ownership:** a payload filter has no answer for "this user
          is gone". The caller finds each record that matches and deletes it,
          one record at a time.
        - **With ownership:** each path segment owns the blocks below it. Thus
          one call deletes the full scope, and Genie removes the keyed bindings
          together with their blocks.

        Args:
            session: Scope to retire, at the level that its identifiers name.

        Returns:
            The removed memories, in ascending order of creation time.
        """

        node = self.scope_node(session)
        if node is None:
            return []
        removed = self.memories(session)
        self.structures.tiers.delete_subtree(node)
        return removed

    def scope_node(self, session: Session) -> MemoryBlock | None:
        """Return the block that owns the scope of one session, if it exists.

        Args:
            session: Scope to find.

        Returns:
            The deepest path block, or `None` if that path does not exist. The
            store creates the path when it stores the first memory of the
            session.
        """

        node = self.structures.memory_root
        for segment in session.path:
            child = self._segment(node, segment)
            if child is None:
                return None
            node = child
        return node

    def ensure_scope(self, session: Session) -> MemoryBlock:
        """Return the block that owns the scope of one session, and create the path if necessary.

        Args:
            session: Scope for a new memory.

        Returns:
            The deepest path block. The method first creates each path segment
            that does not exist.
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
        """Return the memory blocks that Genie selects for one scope."""

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
        """Return the path child with this segment, if it exists."""

        return next(
            (
                child
                for child in self.memory.children(parent)
                if child.type == SCOPE_TYPE and child.metadata.get("segment") == segment
            ),
            None,
        )

    def _sorted(self, blocks: Iterable[MemoryBlock]) -> list[StoredMemory]:
        """Decode blocks into memories, in a stable order that is the same in each run."""

        memories = [memory_from_block(block) for block in blocks]
        return sorted(memories, key=lambda memory: (memory.created_at, memory.text))


def _positive(value: object, *, field: str) -> int:
    """Return a positive integer bound.

    Args:
        value: Bound to check.
        field: Field name for error messages.

    Returns:
        The validated integer.

    Raises:
        TypeError: If the value is a boolean or not an integer.
        ValueError: If the value is not positive.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer.")
    if value < 1:
        raise ValueError(f"{field} must be greater than zero.")
    return value


def _fraction(value: object, *, field: str) -> float:
    """Return a score bound from zero to one.

    Args:
        value: Bound to check.
        field: Field name for error messages.

    Returns:
        The validated float.

    Raises:
        TypeError: If the value is a boolean or not a number.
        ValueError: If the value is not from zero to one.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} must be between zero and one.")
    return float(value)
