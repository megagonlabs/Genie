"""MemoryOS-inspired three-tier memory built from Genie primitives."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any, Callable, Literal, Protocol, cast
from uuid import uuid4

from genie import MemoryBlock, MemoryHandle, MemoryLink
from genie.backend import AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore
from genie.structures import (
    EventLog,
    KeyedCollection,
    LineageDAG,
    MemoryAddress,
    OwnershipTree,
    PropertyGraph,
    TemporalCollection,
)

SHORT_TERM_TYPE = "memoryos.short_term_qa"
MID_TERM_TYPE = "memoryos.mid_term_page"
PROFILE_TYPE = "memoryos.long_term_profile"
KNOWLEDGE_TYPE = "memoryos.long_term_knowledge"

NEXT_PAGE_RELATION = "memoryos.next_page"
PROMOTED_TO_RELATION = "memoryos.promoted_to"
GRAPH_RELATIONS = frozenset({NEXT_PAGE_RELATION, PROMOTED_TO_RELATION})

KnowledgeOwner = Literal["user", "assistant"]
MemoryLayer = Literal["short_term", "mid_term", "user_knowledge", "assistant_knowledge"]
GraphDirection = Literal["out", "in"]

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SEARCH_LIMIT = 1_000
_STRUCTURE_TYPE = "memoryos.structure"
_CONVERSATION_NAMESPACE = ("conversations",)
_PROFILE_ADDRESS = MemoryAddress(("long_term",), "user_profile")


@dataclass(frozen=True, slots=True)
class ShortTermQA:
    """One recent user/assistant interaction in the FIFO queue."""

    block: MemoryBlock
    key: str
    user_input: str
    agent_response: str
    occurred_at: datetime
    sequence: int
    entities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MidTermPage:
    """One summarized page in a compact one-page mid-term session."""

    block: MemoryBlock
    session_id: str
    source_key: str
    summary: str
    user_input: str
    agent_response: str
    occurred_at: datetime
    sequence: int
    segment_heat: float
    segment_visit_count: int
    entities: tuple[str, ...]

    @property
    def id(self) -> str:
        """Return the page id, which is also its Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class LongTermProfile:
    """Current long-term user-profile dictionary."""

    block: MemoryBlock
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    """One item in the user or assistant long-term knowledge collection."""

    block: MemoryBlock
    owner: KnowledgeOwner
    knowledge: str
    timestamp: datetime
    sequence: int
    entities: tuple[str, ...]
    source_page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeDraft:
    """Deterministic stand-in for one fact extracted from a hot session."""

    knowledge: str
    source_page_ids: tuple[str, ...]
    entities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AddMemoryResult:
    """Result of adding one QA pair and processing FIFO overflow."""

    qa: ShortTermQA
    promoted_pages: tuple[MidTermPage, ...]


@dataclass(frozen=True, slots=True)
class RetrievalBundle:
    """Relevant context retrieved from every MemoryOS-inspired layer."""

    short_term: tuple[ShortTermQA, ...]
    mid_term: tuple[MidTermPage, ...]
    user_profile: LongTermProfile | None
    user_knowledge: tuple[KnowledgeEntry, ...]
    assistant_knowledge: tuple[KnowledgeEntry, ...]


@dataclass(frozen=True, slots=True)
class TemporalResult:
    """One dated item from a memory layer."""

    layer: MemoryLayer
    item_id: str
    occurred_at: datetime
    text: str


@dataclass(frozen=True, slots=True)
class GraphResult:
    """One block reached through page chronology or promotion links."""

    block: MemoryBlock
    relation: str
    direction: GraphDirection
    distance: int
    description: str


class MemoryOSStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities required by the MemoryOS structure bundle."""


@dataclass(frozen=True, slots=True)
class MemoryOSStructures:
    """Composable structural views for the three-tier memory system."""

    tree: OwnershipTree
    keys: KeyedCollection
    short_term: EventLog
    mid_term: EventLog
    user_knowledge: EventLog
    assistant_knowledge: EventLog
    timeline: TemporalCollection
    graph: PropertyGraph
    page_lineage: LineageDAG

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: MemoryOSStructureStore,
    ) -> MemoryOSStructures:
        """Build or reopen all structural anchors below one bounded root."""

        if memory.root_id is None:
            raise ValueError("MemoryOSStructures requires a bounded MemoryHandle.")
        tree = OwnershipTree(memory, root=memory.root_id)

        def log_anchor(name: str) -> MemoryBlock:
            existing = next(
                (
                    child
                    for child in tree.children()
                    if child.type == _STRUCTURE_TYPE
                    and child.value == {"kind": name}
                ),
                None,
            )
            if existing is not None:
                return existing
            anchor_id = f"{tree.root_id}:memoryos:{name}"
            return tree.create_child(
                id=anchor_id,
                type=_STRUCTURE_TYPE,
                value={"kind": name},
            )

        short_term = log_anchor("short_term")
        mid_term = log_anchor("mid_term")
        user_knowledge = log_anchor("user_knowledge")
        assistant_knowledge = log_anchor("assistant_knowledge")
        return cls(
            tree=tree,
            keys=KeyedCollection(memory, store, collection=tree.root),
            short_term=EventLog(memory, store, log=short_term),
            mid_term=EventLog(memory, store, log=mid_term),
            user_knowledge=EventLog(memory, store, log=user_knowledge),
            assistant_knowledge=EventLog(memory, store, log=assistant_knowledge),
            timeline=TemporalCollection(memory, store, collection=tree.root),
            graph=PropertyGraph(memory),
            page_lineage=LineageDAG(memory, relation=NEXT_PAGE_RELATION),
        )


class MemoryOSMemory:
    """Compact MemoryOS-inspired hierarchy over a bounded Genie handle."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: MemoryOSStructures,
        short_term_capacity: int = 3,
        heat_threshold: float = 2.0,
        knowledge_capacity: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a layered memory service.

        Args:
            memory: Bounded handle owning all memory tiers.
            structures: Tree, keyed, log, temporal, and graph views.
            short_term_capacity: Maximum QA pairs retained before FIFO promotion.
            heat_threshold: Minimum session heat required for long-term consolidation.
            knowledge_capacity: Maximum entries retained per knowledge owner.
            clock: Optional UTC clock for deterministic tests.
        """

        if not isinstance(short_term_capacity, int) or isinstance(short_term_capacity, bool) or short_term_capacity < 1:
            raise ValueError("short_term_capacity must be an integer of at least 1.")
        if not isinstance(heat_threshold, (int, float)) or isinstance(heat_threshold, bool) or heat_threshold <= 0:
            raise ValueError("heat_threshold must be greater than zero.")
        if not isinstance(knowledge_capacity, int) or isinstance(knowledge_capacity, bool) or knowledge_capacity < 1:
            raise ValueError("knowledge_capacity must be an integer of at least 1.")
        self.memory = memory
        self.structures = structures
        self.short_term_capacity = short_term_capacity
        self.heat_threshold = float(heat_threshold)
        self.knowledge_capacity = knowledge_capacity
        self._clock = clock or (lambda: datetime.now(UTC))

    def add_memory(
        self,
        *,
        key: str,
        user_input: str,
        agent_response: str,
        occurred_at: datetime,
        summary: str,
        entities: Sequence[str] = (),
    ) -> AddMemoryResult:
        """Append a QA pair and promote oldest overflow into mid-term pages."""

        normalized_key = _required_text(key, field="key")
        address = MemoryAddress(_CONVERSATION_NAMESPACE, normalized_key)
        if self.structures.keys.get(address) is not None:
            raise ValueError(f"Memory key {normalized_key!r} already exists.")
        normalized_user = _required_text(user_input, field="user_input")
        normalized_agent = _required_text(agent_response, field="agent_response")
        normalized_summary = _required_text(summary, field="summary")
        normalized_occurred_at = _normalize_timestamp(occurred_at, field="occurred_at")
        normalized_entities = _normalized_strings(entities, field="entities")
        payload = {
            "key": normalized_key,
            "user_input": normalized_user,
            "agent_response": normalized_agent,
            "timestamp": _format_timestamp(normalized_occurred_at),
        }
        block = self.structures.tree.create_child(
            parent=self.structures.short_term.log,
            type=SHORT_TERM_TYPE,
            value=payload,
            metadata={
                "summary": normalized_summary,
                "entities": list(normalized_entities),
                "occurred_at": _format_timestamp(normalized_occurred_at),
            },
        )
        try:
            logged = self.structures.short_term.append(block)
            self.structures.keys.bind(address, block)
            self.structures.timeline.add(
                block,
                occurred_at=normalized_occurred_at,
            )
        except Exception:
            self.memory.delete(block)
            raise
        qa = _qa_from_block(block, sequence=logged.entry.sequence + 1)
        promoted: list[MidTermPage] = []
        while len(self.short_term()) > self.short_term_capacity:
            oldest = self.short_term()[0]
            promoted.append(self._promote_qa(oldest))
        return AddMemoryResult(qa=qa, promoted_pages=tuple(promoted))

    def short_term(self) -> list[ShortTermQA]:
        """Return the FIFO queue from oldest to newest."""

        return [
            _qa_from_block(item.block, sequence=item.entry.sequence + 1)
            for item in self.structures.short_term.read(limit=_SEARCH_LIMIT)
        ]

    def mid_term(self) -> list[MidTermPage]:
        """Return summarized pages in dialogue order."""

        return [
            _page_from_block(item.block, sequence=item.entry.sequence + 1)
            for item in self.structures.mid_term.read(limit=_SEARCH_LIMIT)
        ]

    def retrieve_memory(self, query: str, *, limit: int = 5) -> RetrievalBundle:
        """Retrieve context from short, mid, profile, and both knowledge stores.

        Matching pages increment the visit count and heat of their one-page
        sessions. This is a transparent approximation of MemoryOS segment heat.
        """

        normalized_query = _required_text(query, field="query")
        normalized_limit = _validate_limit(limit)
        query_tokens = _tokens(normalized_query)

        ranked_pages: list[tuple[float, MidTermPage]] = []
        for page in self.mid_term():
            score = _overlap_score(query_tokens, _page_text(page))
            if score > 0:
                ranked_pages.append((score, page))
        ranked_pages.sort(key=lambda pair: (-pair[0], pair[1].sequence))
        heated_pages = tuple(
            self._record_page_visit(page) for _, page in ranked_pages[:normalized_limit]
        )

        def ranked_knowledge(owner: KnowledgeOwner) -> tuple[KnowledgeEntry, ...]:
            ranked = [
                (_overlap_score(query_tokens, entry.knowledge), entry)
                for entry in self.knowledge(owner)
            ]
            ranked = [pair for pair in ranked if pair[0] > 0]
            ranked.sort(key=lambda pair: (-pair[0], pair[1].sequence))
            return tuple(entry for _, entry in ranked[:normalized_limit])

        return RetrievalBundle(
            short_term=tuple(self.short_term()),
            mid_term=heated_pages,
            user_profile=self.get_user_profile(),
            user_knowledge=ranked_knowledge("user"),
            assistant_knowledge=ranked_knowledge("assistant"),
        )

    def update_long_term(
        self,
        *,
        profile_updates: dict[str, Any],
        profile_source_page_ids: Sequence[str],
        user_knowledge: Sequence[KnowledgeDraft] = (),
        assistant_knowledge: Sequence[KnowledgeDraft] = (),
    ) -> tuple[LongTermProfile, tuple[KnowledgeEntry, ...], tuple[KnowledgeEntry, ...]]:
        """Consolidate pages from hot one-page sessions into long-term stores."""

        profile_sources = self._require_heated_pages(profile_source_page_ids)
        existing = self.get_user_profile()
        profile_data = deepcopy(existing.data if existing is not None else {})
        profile_data.update(deepcopy(profile_updates))
        if existing is None:
            profile_block = self.structures.tree.create_child(
                type=PROFILE_TYPE,
                value=profile_data,
                metadata={"kind": "user_profile"},
            )
            try:
                self.structures.keys.bind(_PROFILE_ADDRESS, profile_block)
            except Exception:
                self.memory.delete(profile_block)
                raise
        else:
            profile_block = self.memory.update(existing.block, value=deepcopy(profile_updates))
        profile = _profile_from_block(profile_block)
        for page in profile_sources:
            self._add_link_unique(
                page.block,
                profile.block,
                PROMOTED_TO_RELATION,
                metadata={"target": "user_profile"},
            )

        user_entries = tuple(self._add_knowledge("user", draft) for draft in user_knowledge)
        assistant_entries = tuple(
            self._add_knowledge("assistant", draft) for draft in assistant_knowledge
        )
        return profile, user_entries, assistant_entries

    def get_user_profile(self) -> LongTermProfile | None:
        """Return the current long-term profile dictionary."""

        block = self.structures.keys.get(_PROFILE_ADDRESS)
        return _profile_from_block(block) if block is not None else None

    def knowledge(self, owner: KnowledgeOwner) -> list[KnowledgeEntry]:
        """Return one bounded long-term knowledge collection in insertion order."""

        normalized_owner = _validate_owner(owner)
        log = self._knowledge_log(normalized_owner)
        return [
            _knowledge_from_block(item.block, sequence=item.entry.sequence + 1)
            for item in log.read(limit=_SEARCH_LIMIT)
        ]

    def retrieve_temporal(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int = 20,
    ) -> list[TemporalResult]:
        """Return dated QA, page, and knowledge entries in an inclusive interval."""

        normalized_start = _normalize_timestamp(start, field="start")
        normalized_end = _normalize_timestamp(end, field="end")
        if normalized_start > normalized_end:
            raise ValueError("start must be earlier than or equal to end.")
        normalized_limit = _validate_limit(limit)
        try:
            exclusive_end = normalized_end + timedelta(microseconds=1)
        except OverflowError:
            exclusive_end = None
        results: list[TemporalResult] = []
        for temporal in self.structures.timeline.events(
            start=normalized_start,
            stop=exclusive_end,
            limit=_SEARCH_LIMIT,
        ):
            occurred_at = temporal.entry.occurred_at
            if occurred_at is None or occurred_at > normalized_end:
                continue
            block = temporal.block
            if block.type == SHORT_TERM_TYPE:
                qa = _qa_from_block(block, sequence=0)
                results.append(
                    TemporalResult(
                        "short_term",
                        block.id,
                        occurred_at,
                        f"User: {qa.user_input} Assistant: {qa.agent_response}",
                    )
                )
            elif block.type == MID_TERM_TYPE:
                page = _page_from_block(block, sequence=0)
                results.append(
                    TemporalResult("mid_term", block.id, occurred_at, page.summary)
                )
            elif block.type == KNOWLEDGE_TYPE:
                entry = _knowledge_from_block(block, sequence=0)
                layer: MemoryLayer = (
                    "user_knowledge"
                    if entry.owner == "user"
                    else "assistant_knowledge"
                )
                results.append(
                    TemporalResult(layer, block.id, occurred_at, entry.knowledge)
                )
        results.sort(key=lambda result: (result.occurred_at, result.layer, result.item_id))
        return results[:normalized_limit]

    def retrieve_graph(
        self,
        anchor_page_id: str,
        *,
        max_depth: int = 1,
        limit: int = 20,
    ) -> list[GraphResult]:
        """Traverse page chronology and mid-to-long promotion links."""

        anchor = self.get_page(anchor_page_id)
        if anchor is None:
            raise KeyError(f"Mid-term page {anchor_page_id!r} was not found.")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
            raise ValueError("max_depth must be an integer of at least 1.")
        normalized_limit = _validate_limit(limit)
        queue: deque[tuple[MemoryBlock, int]] = deque([(anchor.block, 0)])
        visited = {anchor.block.id}
        results: list[GraphResult] = []
        while queue and len(results) < normalized_limit:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            connected: list[tuple[MemoryBlock, str, GraphDirection]] = []
            for neighbor_record in self.structures.graph.neighbors(
                current,
                direction="both",
            ):
                link = neighbor_record.link
                if link.relation not in GRAPH_RELATIONS:
                    continue
                neighbor_id = neighbor_record.block.id
                direction: GraphDirection = neighbor_record.direction
                if neighbor_id in visited:
                    continue
                connected.append((neighbor_record.block, link.relation, direction))
            connected.sort(
                key=lambda value: (
                    self._structural_sequence(value[0]),
                    value[0].type,
                    value[0].id,
                )
            )
            for neighbor, relation, direction in connected:
                if neighbor.id in visited:
                    continue
                visited.add(neighbor.id)
                next_depth = depth + 1
                results.append(
                    GraphResult(
                        block=neighbor,
                        relation=relation,
                        direction=direction,
                        distance=next_depth,
                        description=_describe_block(neighbor),
                    )
                )
                if len(results) >= normalized_limit:
                    break
                queue.append((neighbor, next_depth))
        return results

    def get_page(self, page_id: str) -> MidTermPage | None:
        """Return a mid-term page by stable page id."""

        for page in self.mid_term():
            if page.id == page_id:
                return page
        return None

    def _promote_qa(self, qa: ShortTermQA) -> MidTermPage:
        previous = self.mid_term()
        page_id = f"page_{uuid4().hex}"
        session_id = f"session_{uuid4().hex}"
        summary = cast(str, qa.block.metadata["summary"])
        payload = {
            "page_id": page_id,
            "session_id": session_id,
            "source_qa_key": qa.key,
            "summary": summary,
            "dialogue": {
                "user_input": qa.user_input,
                "agent_response": qa.agent_response,
                "timestamp": _format_timestamp(qa.occurred_at),
            },
            "H_segment": 1.0,
            "N_visit": 0,
        }
        block = self.structures.tree.create_child(
            parent=self.structures.mid_term.log,
            id=page_id,
            type=MID_TERM_TYPE,
            value=payload,
            metadata={
                "segment_heat": 1.0,
                "occurred_at": _format_timestamp(qa.occurred_at),
                "entities": list(qa.entities),
            },
        )
        try:
            logged = self.structures.mid_term.append(block)
            self.structures.timeline.add(block, occurred_at=qa.occurred_at)
            page = _page_from_block(block, sequence=logged.entry.sequence + 1)
            if previous:
                self.structures.page_lineage.add(
                    previous[-1].block,
                    page.block,
                    metadata={"sequence": "next"},
                )
            self.structures.keys.rebind(
                MemoryAddress(_CONVERSATION_NAMESPACE, qa.key),
                page.block,
                expected=qa.block,
            )
        except Exception:
            self.memory.delete(block)
            raise
        self.memory.delete(qa.block)
        return page

    def _record_page_visit(self, page: MidTermPage) -> MidTermPage:
        updated_heat = page.segment_heat + 1.0
        block = self.memory.update(
            page.block,
            value={"H_segment": updated_heat, "N_visit": page.segment_visit_count + 1},
            metadata={"segment_heat": updated_heat},
        )
        return _page_from_block(block, sequence=page.sequence)

    def _require_heated_pages(self, page_ids: Sequence[str]) -> tuple[MidTermPage, ...]:
        normalized_ids = _normalized_strings(page_ids, field="source_page_ids")
        if not normalized_ids:
            raise ValueError("Long-term consolidation requires at least one source page.")
        pages: list[MidTermPage] = []
        for page_id in normalized_ids:
            page = self.get_page(page_id)
            if page is None:
                raise ValueError(f"Source page {page_id!r} does not exist.")
            if page.segment_heat < self.heat_threshold:
                raise ValueError(
                    f"Source page {page_id!r} belongs to a segment with heat "
                    f"{page.segment_heat:.1f}, below "
                    f"threshold {self.heat_threshold:.1f}."
                )
            pages.append(page)
        return tuple(pages)

    def _add_knowledge(self, owner: KnowledgeOwner, draft: KnowledgeDraft) -> KnowledgeEntry:
        normalized_owner = _validate_owner(owner)
        fact = _required_text(draft.knowledge, field="knowledge")
        source_pages = self._require_heated_pages(draft.source_page_ids)
        entities = _normalized_strings(draft.entities, field="entities")
        for existing in self.knowledge(normalized_owner):
            if existing.knowledge == fact:
                for page in source_pages:
                    self._add_link_unique(
                        page.block,
                        existing.block,
                        PROMOTED_TO_RELATION,
                        metadata={"target": f"{normalized_owner}_knowledge"},
                    )
                return existing
        timestamp = _normalize_timestamp(self._clock(), field="clock")
        payload = {
            "owner": normalized_owner,
            "knowledge": fact,
            "timestamp": _format_timestamp(timestamp),
            "source_page_ids": [page.id for page in source_pages],
        }
        log = self._knowledge_log(normalized_owner)
        block = self.structures.tree.create_child(
            parent=log.log,
            type=KNOWLEDGE_TYPE,
            value=payload,
            metadata={
                "owner": normalized_owner,
                "occurred_at": _format_timestamp(timestamp),
                "entities": list(entities),
            },
        )
        try:
            logged = log.append(block)
            self.structures.timeline.add(block, occurred_at=timestamp)
        except Exception:
            self.memory.delete(block)
            raise
        entry = _knowledge_from_block(block, sequence=logged.entry.sequence + 1)
        for page in source_pages:
            self._add_link_unique(
                page.block,
                entry.block,
                PROMOTED_TO_RELATION,
                metadata={"target": f"{normalized_owner}_knowledge"},
            )
        while len(self.knowledge(normalized_owner)) > self.knowledge_capacity:
            self.memory.delete(self.knowledge(normalized_owner)[0].block)
        return entry

    def _add_link_unique(
        self,
        source: MemoryBlock,
        target: MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        for link in self.structures.graph.edges(source, relation=relation, direction="out"):
            if link.target_id == target.id and link.metadata == metadata:
                return link
        return self.structures.graph.add_edge(
            source,
            target,
            relation,
            metadata=metadata,
        )

    def _knowledge_log(self, owner: KnowledgeOwner) -> EventLog:
        """Return the ordered log for one long-term knowledge owner."""

        return (
            self.structures.user_knowledge
            if owner == "user"
            else self.structures.assistant_knowledge
        )

    def _structural_sequence(self, block: MemoryBlock) -> int:
        """Return a log position for stable mixed graph-result ordering."""

        logs = (
            self.structures.mid_term,
            self.structures.user_knowledge,
            self.structures.assistant_knowledge,
        )
        for log in logs:
            for item in log.read(limit=_SEARCH_LIMIT):
                if item.block.id == block.id:
                    return item.entry.sequence
        return _SEARCH_LIMIT + 1


def _qa_from_block(block: MemoryBlock, *, sequence: int) -> ShortTermQA:
    if block.type != SHORT_TERM_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a short-term QA pair.")
    value = cast(dict[str, Any], block.value)
    if set(value) != {"key", "user_input", "agent_response", "timestamp"}:
        raise TypeError(f"Block {block.id!r} has an invalid QA-pair shape.")
    return ShortTermQA(
        block=block,
        key=cast(str, value["key"]),
        user_input=cast(str, value["user_input"]),
        agent_response=cast(str, value["agent_response"]),
        occurred_at=_parse_timestamp(cast(str, value["timestamp"]), field="timestamp"),
        sequence=sequence,
        entities=tuple(cast(list[str], block.metadata.get("entities", []))),
    )


def _page_from_block(block: MemoryBlock, *, sequence: int) -> MidTermPage:
    if block.type != MID_TERM_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a mid-term page.")
    value = cast(dict[str, Any], block.value)
    dialogue = cast(dict[str, Any], value["dialogue"])
    return MidTermPage(
        block=block,
        session_id=cast(str, value["session_id"]),
        source_key=cast(str, value["source_qa_key"]),
        summary=cast(str, value["summary"]),
        user_input=cast(str, dialogue["user_input"]),
        agent_response=cast(str, dialogue["agent_response"]),
        occurred_at=_parse_timestamp(cast(str, dialogue["timestamp"]), field="timestamp"),
        sequence=sequence,
        segment_heat=float(value["H_segment"]),
        segment_visit_count=cast(int, value["N_visit"]),
        entities=tuple(cast(list[str], block.metadata.get("entities", []))),
    )


def _profile_from_block(block: MemoryBlock) -> LongTermProfile:
    if block.type != PROFILE_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a long-term profile.")
    return LongTermProfile(block=block, data=deepcopy(cast(dict[str, Any], block.value)))


def _knowledge_from_block(block: MemoryBlock, *, sequence: int) -> KnowledgeEntry:
    if block.type != KNOWLEDGE_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a long-term knowledge entry.")
    value = cast(dict[str, Any], block.value)
    return KnowledgeEntry(
        block=block,
        owner=_validate_owner(value["owner"]),
        knowledge=cast(str, value["knowledge"]),
        timestamp=_parse_timestamp(cast(str, value["timestamp"]), field="timestamp"),
        sequence=sequence,
        entities=tuple(cast(list[str], block.metadata.get("entities", []))),
        source_page_ids=tuple(cast(list[str], value["source_page_ids"])),
    )


def _describe_block(block: MemoryBlock) -> str:
    if block.type == MID_TERM_TYPE:
        page = _page_from_block(block, sequence=0)
        return f"Mid-term page: {page.summary}"
    if block.type == PROFILE_TYPE:
        return f"User profile: {json.dumps(block.value, sort_keys=True)}"
    if block.type == KNOWLEDGE_TYPE:
        entry = _knowledge_from_block(block, sequence=0)
        return f"{entry.owner.title()} knowledge: {entry.knowledge}"
    return f"{block.type}: {block.id}"


def _page_text(page: MidTermPage) -> str:
    return f"{page.summary} {page.user_input} {page.agent_response}"


def _validate_owner(value: object) -> KnowledgeOwner:
    if value not in {"user", "assistant"}:
        raise ValueError("knowledge owner must be 'user' or 'assistant'.")
    return cast(KnowledgeOwner, value)


def _normalized_strings(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required_text(value, field=field)
        marker = item.casefold()
        if marker not in seen:
            seen.add(marker)
            normalized.append(item)
    return tuple(normalized)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value.strip()


def _normalize_timestamp(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    return _normalize_timestamp(parsed, field=field)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(value.casefold()))


def _overlap_score(query_tokens: set[str], value: str) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens.intersection(_tokens(value))) / len(query_tokens)


def _validate_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("limit must be an integer of at least 1.")
    return min(value, _SEARCH_LIMIT)
