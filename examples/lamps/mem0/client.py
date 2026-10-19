"""The public surface of Mem0, made from the 4 parts below it.

Upstream, this surface is one class:

- **`add`:** runs the 2 phases and writes their decisions.
- **`search`:** returns the memories that match, with their relationships.
- **`get`, `update`, and `delete`:** operate on a memory id.
- **`get_all` and `delete_all`:** operate on a session.
- **`history`:** reads the changes to one memory.

This module keeps that form in one place. The store, the history log, the
graph, and the pipeline each know one thing. This module joins all 4 parts for
each call.

2 behaviors come from the composition, not from one part:

- **Neighbor lookup for each candidate:** the client does the neighbor lookup
  again for each candidate in a call. Thus a candidate sees the memories that
  earlier candidates of the same call wrote. 2 wordings of the same fact give
  an `ADD` and then an `UPDATE`, not 2 adds. The extractor removes exact
  repeats in a call before this step.
- **A delete keeps the history:** a memory that the client removes keeps its
  history, because the history tier owns the recorded events. A delete is the
  last event that a caller can read about a memory. It does not stop the reads
  of that history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from examples.lamps.mem0.graph import GraphMemory
from examples.lamps.mem0.history import HistoryLog
from examples.lamps.mem0.model import (
    FACTUAL,
    PROCEDURAL,
    READ_LIMIT,
    AddResult,
    Decision,
    Fact,
    GraphRelation,
    HistoryEntry,
    MemoryChange,
    Message,
    SearchResult,
    Session,
    StoredMemory,
    Triplet,
    normalize_timestamp,
    required_text,
)
from examples.lamps.mem0.pipeline import FactExtractor, MemoryUpdater
from examples.lamps.mem0.store import MemoryStore
from genie import MemoryHandle


class Mem0Memory:
    """A Mem0-shaped memory client over one bounded Genie handle."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        store: MemoryStore,
        history_log: HistoryLog,
        graph: GraphMemory,
        extractor: FactExtractor | None = None,
        updater: MemoryUpdater | None = None,
    ) -> None:
        """Compose one memory client from its 4 parts.

        Args:
            memory: Bounded Genie handle under the parts.
            store: Scoped memory records.
            history_log: Recorded changes to those records.
            graph: Entities and their relationships.
            extractor: Phase-one extraction. The default is `FactExtractor()`.
            updater: Phase-two decision. The default is `MemoryUpdater()`.
        """

        self.memory = memory
        self.store = store
        self.history_log = history_log
        self.graph = graph
        self.extractor = extractor or FactExtractor()
        self.updater = updater or MemoryUpdater()

    def add(
        self,
        session: Session,
        messages: Sequence[Message] = (),
        *,
        facts: Sequence[Fact] = (),
        relations: Sequence[Triplet] = (),
        infer: bool = True,
        memory_type: str = FACTUAL,
        metadata: Mapping[str, Any] | None = None,
        at: datetime,
    ) -> AddResult:
        """Send one exchange through the pipeline and write its decisions.

        The 3 paths through this method are the 3 paths that Mem0 has:

        - **`infer` is `True`:** the extracted candidates go through the update
          phase. They can add, replace, or remove memories.
        - **`infer` is `False`:** the method stores the turns in their original
          words. Each turn becomes a new memory.
        - **Procedural write:** the method stores one summary of the full
          exchange and does not compare it with the stored memories. A summary
          of a task trajectory is not a different wording of another memory.
          This path does not apply `relations`.

        Args:
            session: Scope to write in.
            messages: Turns in the exchange.
            facts: Candidates, as a model would extract them. For a procedural
                write, the one summary.
            relations: Candidate relationships for the graph half.
            infer: If `True`, the method runs extraction and the update phase,
                as upstream.
            memory_type: `factual` or `procedural_memory`.
            metadata: Application metadata to keep and index with each memory.
            at: Time that the method records on each write of this call.

        Returns:
            The result for each candidate and for each relationship.

        Raises:
            ValueError: If a procedural write does not have exactly 1 summary,
                or if the call stores a new memory with an unknown memory type.
        """

        written_at = normalize_timestamp(at, field="at")
        turns = tuple(messages)
        if memory_type == PROCEDURAL:
            candidates = self.extractor.extract(turns, facts)
            if len(candidates) != 1:
                raise ValueError("A procedural write stores exactly one summary of the exchange.")
            change = self._add(session, candidates[0], kind=PROCEDURAL, metadata=metadata, at=written_at)
            return AddResult(session=session, changes=(change,))
        candidates = self.extractor.verbatim(turns) if not infer else self.extractor.extract(turns, facts)
        changes: list[MemoryChange] = []
        for candidate in candidates:
            decision = (
                Decision(fact=candidate, event="ADD", target_id=None, score=0.0, reason="stored as said, without extraction")
                if not infer
                else self.updater.decide(candidate, self.store.neighbors(session, candidate.text, limit=self.updater.neighbors))
            )
            changes.append(self._apply(session, decision, kind=memory_type, metadata=metadata, at=written_at))
        return AddResult(
            session=session,
            changes=tuple(changes),
            relations=tuple(self.graph.apply(session, triplet, at=written_at) for triplet in relations),
        )

    def search(
        self,
        session: Session,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        threshold: float = 0.0,
    ) -> SearchResult:
        """Return the memories that match a query, with their relationships.

        Args:
            session: Scope to search below.
            query: Free text. The method matches its terms against the memory
                text.
            filters: Metadata filter for the Genie search.
            limit: Maximum number of memories to return.
            threshold: Minimum score of a memory.

        Returns:
            The 2 halves of the search. Upstream also returns them together.
        """

        text = required_text(query, field="query")
        return SearchResult(
            query=text,
            hits=tuple(self.store.search(session, text, filters=filters, limit=limit, threshold=threshold)),
            relations=tuple(self.graph.relations_for(session, text)),
        )

    def get(self, memory_id: str) -> StoredMemory | None:
        """Find one memory by its id, with no scope.

        Args:
            memory_id: Id to find.

        Returns:
            The memory, or `None` if the id is unknown.
        """

        return self.store.get(memory_id)

    def owner(self, memory_id: str) -> tuple[str, ...] | None:
        """Return the scope path of one memory id.

        - **Why:** the id space is flat. A caller uses this lookup to decide if
          an id that it received is in the scope that the caller can access.
        - **Source:** the history tier gives the answer, not the store. Thus a
          deleted id still gives its scope, and a caller cannot think that it
          is an unused id.

        Args:
            memory_id: Id to find.

        Returns:
            The path, or `None` if no memory with that id had a write.
        """

        return self.history_log.owner(memory_id)

    def get_all(
        self,
        session: Session,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = READ_LIMIT,
    ) -> list[StoredMemory]:
        """Return all memories in the scope of one session.

        Args:
            session: Scope to read. A session that names only a user returns
                the memories of all agents and runs below that user.
            filters: Metadata filter for the Genie search.
            limit: Maximum number of memories to return.

        Returns:
            Memories in ascending order of creation time, and then of text.
        """

        return self.store.memories(session, filters=filters, limit=limit)

    def update(
        self,
        memory_id: str,
        text: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        actor_id: str | None = None,
        role: str | None = None,
        at: datetime,
    ) -> MemoryChange:
        """Rewrite one memory because the caller asks for it, not the pipeline.

        Mem0 has this call in addition to the inferred path, so an application
        can correct a memory directly. The write and its history row are the
        same as for an `UPDATE` from the update phase. Only the reason is
        different.

        Args:
            memory_id: Id of the memory to rewrite.
            text: New text.
            metadata: Application metadata to merge into the record.
            actor_id: Speaker that asked for the change.
            role: Role of the turn with the request.
            at: Time that the method records on the write.

        Returns:
            The applied change.

        Raises:
            KeyError: If no memory has that id.
        """

        target = self.store.require(memory_id)
        fact = Fact(text=text, actor_id=actor_id, role=role)
        decision = Decision(
            fact=fact,
            event="UPDATE",
            target_id=target.memory_id,
            score=self.updater.similarity(fact, target),
            reason="the caller rewrote the memory directly",
        )
        return self._apply(target.session, decision, kind=target.kind, metadata=metadata, at=at)

    def delete(self, memory_id: str, *, actor_id: str | None = None, role: str | None = None, at: datetime) -> MemoryChange:
        """Delete one memory because the caller asks for it.

        Args:
            memory_id: Id of the memory to delete.
            actor_id: Speaker that asked for the delete.
            role: Role of the turn with the request.
            at: Time that the method records on the history row.

        Returns:
            The applied change, with the deleted memory.

        Raises:
            KeyError: If no memory has that id.
        """

        target = self.store.require(memory_id)
        decision = Decision(
            fact=Fact(text=target.text, retracts=True, actor_id=actor_id, role=role),
            event="DELETE",
            target_id=target.memory_id,
            score=1.0,
            reason="the caller deleted the memory directly",
        )
        return self._apply(target.session, decision, kind=target.kind, metadata=None, at=at)

    def delete_all(self, session: Session, *, at: datetime) -> list[MemoryChange]:
        """Retire one scope and all memories below it.

        - **Not a filter operation:** a payload filter cannot give this
          operation.
        - **One subtree delete:** each session segment owns the memories below
          it. Thus the retire of a user, an agent, or a single run is one
          subtree delete. It is not a scan with one delete for each record.
        - **History:** each removed memory still has a history that records its
          delete.

        Args:
            session: Scope to retire, at the level that its identifiers name.
            at: Time that the method records on each history row.

        Returns:
            One change for each removed memory, in ascending order of creation
            time. If the session names a full entity namespace, the method also
            clears its graph half. Upstream also clears the graph memory
            together with the vector store.
        """

        applied_at = normalize_timestamp(at, field="at")
        removed = self.store.drop(session)
        self.graph.drop(session)
        changes: list[MemoryChange] = []
        for memory in removed:
            self.history_log.record(
                memory.memory_id,
                "DELETE",
                session=memory.session,
                old_memory=memory.text,
                actor_id=memory.session.actor_id,
                at=applied_at,
            )
            changes.append(
                MemoryChange(
                    decision=Decision(
                        fact=Fact(text=memory.text, retracts=True),
                        event="DELETE",
                        target_id=memory.memory_id,
                        score=1.0,
                        reason=f"the {session.label!r} scope was retired",
                    ),
                    memory=memory,
                    previous_text=memory.text,
                )
            )
        return changes

    def history(self, memory_id: str) -> list[HistoryEntry]:
        """Return all recorded changes to one memory, the oldest first.

        Args:
            memory_id: Memory to read the history of.

        Returns:
            Events in ascending revision order. A deleted memory keeps its
            history, and the delete is its last event.
        """

        return self.history_log.history(memory_id)

    def journal(self, *, limit: int = READ_LIMIT) -> list[HistoryEntry]:
        """Return all recorded changes to all memories, in the order that the lamp applied them.

        Args:
            limit: Maximum number of events to return.

        Returns:
            Events in ascending journal order.
        """

        return self.history_log.journal(limit=limit)

    def relations(self, session: Session, *, include_retracted: bool = False) -> list[GraphRelation]:
        """Return the relationships in the entity namespace of one scope.

        Args:
            session: Scope to read.
            include_retracted: If `True`, the result also includes the
                relationships that are marked as no longer true.

        Returns:
            Relationships in ascending order of source, relation, and target.
        """

        return self.graph.relations(session, include_retracted=include_retracted)

    def _apply(
        self,
        session: Session,
        decision: Decision,
        *,
        kind: str,
        metadata: Mapping[str, Any] | None,
        at: datetime,
    ) -> MemoryChange:
        """Apply one decision and record it in the history, unless the decision is `NONE`."""

        if decision.event == "ADD":
            return self._add(session, decision.fact, kind=kind, metadata=metadata, at=at, decision=decision)
        if decision.event == "NONE":
            return MemoryChange(decision=decision)
        target = self.store.require(str(decision.target_id))
        if decision.event == "DELETE":
            removed = self.store.remove(target.memory_id)
            self.history_log.record(
                target.memory_id,
                "DELETE",
                session=removed.session,
                old_memory=removed.text,
                actor_id=decision.fact.actor_id,
                role=decision.fact.role,
                at=at,
            )
            return MemoryChange(decision=decision, memory=removed, previous_text=removed.text)
        updated = self.store.replace(target.memory_id, decision.fact.text, metadata=metadata, at=at)
        self.history_log.record(
            target.memory_id,
            "UPDATE",
            session=updated.session,
            old_memory=target.text,
            new_memory=updated.text,
            actor_id=decision.fact.actor_id,
            role=decision.fact.role,
            at=at,
        )
        return MemoryChange(decision=decision, memory=updated, previous_text=target.text)

    def _add(
        self,
        session: Session,
        fact: Fact,
        *,
        kind: str,
        metadata: Mapping[str, Any] | None,
        at: datetime,
        decision: Decision | None = None,
    ) -> MemoryChange:
        """Store one new memory and record the `ADD` in the history."""

        stored = self.store.create(
            session.with_actor(fact.actor_id or session.actor_id),
            fact.text,
            kind=kind,
            metadata=metadata,
            at=at,
        )
        self.history_log.record(
            stored.memory_id,
            "ADD",
            session=stored.session,
            new_memory=stored.text,
            actor_id=stored.session.actor_id,
            role=fact.role,
            at=at,
        )
        return MemoryChange(
            decision=decision
            or Decision(
                fact=fact,
                event="ADD",
                target_id=None,
                score=0.0,
                reason="a procedural summary is stored without comparison",
            ),
            memory=stored,
        )
