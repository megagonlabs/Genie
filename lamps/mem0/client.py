"""Mem0's public surface, assembled from the four pieces below it.

Upstream this is one class:

- `add` runs the two phases and writes what they decide.
- `search` returns the matching memories with the relationships beside them.
- `get`, `update`, and `delete` work on a memory id.
- `get_all` and `delete_all` work on a session.
- `history` reads what happened to one memory.

Keeping that shape in one place is the point of this module. The store, the
history log, the graph, and the pipeline each know one thing, and this is where
a call becomes all four.

Two behaviors follow from the composition rather than from any one piece:

- The neighbor lookup is redone for every candidate in a call, so two candidates
  saying the same thing become an add and then a repeat instead of two adds.
- A memory removed here keeps its history, because the recorded events are owned
  by the history tier. A deletion is the last thing you can read about a memory
  rather than the end of reading about it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from genie import MemoryHandle
from lamps.mem0.graph import GraphMemory
from lamps.mem0.history import HistoryLog
from lamps.mem0.model import (
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
from lamps.mem0.pipeline import FactExtractor, MemoryUpdater
from lamps.mem0.store import MemoryStore


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
        """Compose one memory client from its four parts.

        Args:
            memory: Bounded Genie handle the parts were built over.
            store: Scoped memory records.
            history_log: Recorded changes to those records.
            graph: Entities and relationships beside them.
            extractor: Phase-one extraction. Defaults to `FactExtractor()`.
            updater: Phase-two decision. Defaults to `MemoryUpdater()`.
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
        """Run one exchange through the pipeline and write what it decides.

        Three paths lead through here, and they are the three Mem0 offers.

        - `infer` set: the extracted candidates go through the update phase and
          may add, replace, or remove memories.
        - `infer` cleared: the turns are stored as they were said, and every one
          of them is new.
        - A procedural write stores one summary of the whole exchange without
          consulting what is already on file, because a trajectory summary is not
        a competing wording of anything.

        Args:
            session: Scope to write in.
            messages: Turns in the exchange.
            facts: Candidates a model would have extracted, or the single
                summary for a procedural write.
            relations: Candidate relationships for the graph half.
            infer: Whether to run extraction and the update phase, as upstream.
            memory_type: `factual` or `procedural_memory`.
            metadata: Application metadata to keep and index with each memory.
            at: Time recorded on every write this call makes.

        Returns:
            What happened to every candidate, and to every relationship.

        Raises:
            ValueError: If a procedural write does not carry exactly one
                summary, or the memory type is unknown.
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
        """Return the memories matching a query and the relationships beside them.

        Args:
            session: Scope to search below.
            query: Free text whose terms are matched against memory text.
            filters: Metadata filter passed through to Genie.
            limit: Maximum memories to return.
            threshold: Minimum score a memory must reach.

        Returns:
            Both halves of the search, as upstream returns them together.
        """

        text = required_text(query, field="query")
        return SearchResult(
            query=text,
            hits=tuple(self.store.search(session, text, filters=filters, limit=limit, threshold=threshold)),
            relations=tuple(self.graph.relations_for(session, text)),
        )

    def get(self, memory_id: str) -> StoredMemory | None:
        """Look one memory up by id, without naming a scope.

        Args:
            memory_id: Id to look up.

        Returns:
            The memory, or `None` when the id is unknown.
        """

        return self.store.get(memory_id)

    def get_all(
        self,
        session: Session,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = READ_LIMIT,
    ) -> list[StoredMemory]:
        """Return every memory in one session's scope.

        Args:
            session: Scope to read. A session naming only a user returns the
                memories of every agent and run below it.
            filters: Metadata filter passed through to Genie.
            limit: Maximum memories to return.

        Returns:
            Memories in ascending creation-time and text order.
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
        """Rewrite one memory because the caller said so, not the pipeline.

        Mem0 exposes this beside the inferred path so an application can correct
        a memory directly. The write and its history row are the same as an
        `UPDATE` the update phase decided on; only the reason differs.

        Args:
            memory_id: Id of the memory to rewrite.
            text: Replacement text.
            metadata: Application metadata to merge into the record.
            actor_id: Who asked for the change.
            role: Role of the turn it was asked in.
            at: Time recorded on the write.

        Returns:
            The change that was applied.

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
        """Delete one memory because the caller said so.

        Args:
            memory_id: Id of the memory to delete.
            actor_id: Who asked for the deletion.
            role: Role of the turn it was asked in.
            at: Time recorded on the history row.

        Returns:
            The change that was applied, carrying the deleted memory.

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
        """Retire one scope and everything stored below it.

        This is the operation a payload filter cannot give you. Because each
        session segment owns the memories addressed below it, retiring a user,
        an agent, or a single run is one subtree delete rather than a scan and a
        delete per record, and every removed memory still has a history saying
        it was deleted.

        Args:
            session: Scope to retire, at whatever level its identifiers name.
            at: Time recorded on every history row.

        Returns:
            One change per removed memory, in ascending creation-time order.
            Retiring a whole entity namespace also drops its graph half, as
            upstream clears graph memory alongside the vector store.
        """

        applied_at = normalize_timestamp(at, field="at")
        removed = self.store.drop(session)
        self.graph.drop(session)
        changes: list[MemoryChange] = []
        for memory in removed:
            self.history_log.record(
                memory.memory_id,
                "DELETE",
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
        """Return every recorded change to one memory, oldest first.

        Args:
            memory_id: Memory to read the history of.

        Returns:
            Events in ascending revision order. A deleted memory keeps its
            history, ending with the deletion.
        """

        return self.history_log.history(memory_id)

    def journal(self, *, limit: int = READ_LIMIT) -> list[HistoryEntry]:
        """Return every recorded change to any memory, in applied order.

        Args:
            limit: Maximum events to return.

        Returns:
            Events in ascending journal order.
        """

        return self.history_log.journal(limit=limit)

    def relations(self, session: Session, *, include_retracted: bool = False) -> list[GraphRelation]:
        """Return the relationships stored in one scope's entity namespace.

        Args:
            session: Scope to read.
            include_retracted: Whether relationships marked as no longer true
                are returned as well.

        Returns:
            Relationships in ascending source, relation, and target order.
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
        """Carry out one decision and record it, unless it was a `NONE`."""

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
        """Store one new memory and record the addition."""

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
