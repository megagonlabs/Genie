"""Mem0-inspired application memory built from Genie primitives.

This module keeps record meaning, entity linking, retrieval, and mental-model
behavior in the lamp. Genie provides scoped blocks and typed links without
adopting these application-specific conventions.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal, Protocol, Sequence, cast
from uuid import uuid4

from genie import MemoryBlock, MemoryHandle
from genie.backend import KeyedStore, TemporalStore
from genie.structures import KeyedCollection, MemoryAddress, PropertyGraph, TemporalCollection

FACT_RECORD_TYPE = "mem0.fact_record"
MENTAL_MODEL_TYPE = "mem0.mental_model"
ENTITY_LINK_RELATION = "mem0.entity_link"

MemoryKind = Literal["fact", "experience"]
GraphDirection = Literal["out", "in"]

_KINDS = frozenset({"fact", "experience"})
_RESERVED_METADATA = frozenset({"kind", "entities", "occurred_at"})
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SEARCH_LIMIT = 1_000


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Open-source Mem0 session identifiers bound to one tool set."""

    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        """Validate that the scope is useful and contains non-empty ids."""

        for field in ("user_id", "agent_id", "run_id"):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Mem0 scope ids must be non-empty strings or None.")
                object.__setattr__(self, field, value.strip())
        values = (self.user_id, self.agent_id, self.run_id)
        if not any(value is not None for value in values):
            raise ValueError("At least one Mem0 scope id is required.")

    def as_dict(self) -> dict[str, str | None]:
        """Return the session identifiers accepted by open-source Mem0."""

        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class ExtractedFactRecord:
    """Resolved view of one Mem0-inspired extracted-memory record."""

    block: MemoryBlock
    text: str
    scope: MemoryScope
    hash: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def id(self) -> str:
        """Return the record id, which also serves as the Genie block id."""

        return self.block.id

    @property
    def kind(self) -> MemoryKind:
        """Return the lamp-level fact or experience classification."""

        return cast(MemoryKind, self.metadata["kind"])

    @property
    def entities(self) -> tuple[str, ...]:
        """Return normalized entities used for application-owned linking."""

        return tuple(cast(list[str], self.metadata.get("entities", [])))

    @property
    def occurred_at(self) -> datetime | None:
        """Return the event time, which is distinct from ingestion time."""

        value = self.metadata.get("occurred_at")
        if value is None:
            return None
        return _parse_timestamp(cast(str, value), field="metadata.occurred_at")

    def canonical_value(self) -> dict[str, Any]:
        """Return the exact record value stored in the Genie block."""

        return deepcopy(cast(dict[str, Any], self.block.value))


@dataclass(frozen=True, slots=True)
class MentalModel:
    """Application-level synthesized document supported by fact records."""

    block: MemoryBlock
    name: str
    source_query: str
    content: str
    source_memory_ids: tuple[str, ...]
    scope: MemoryScope
    created_at: datetime
    updated_at: datetime

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One ranked result from the portable lexical retrieval strategy."""

    memory: ExtractedFactRecord
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphResult:
    """A fact record reached through one or more shared-entity links."""

    memory: ExtractedFactRecord
    shared_entities: tuple[str, ...]
    direction: GraphDirection
    distance: int


class Mem0StructureStore(KeyedStore, TemporalStore, Protocol):
    """Backend capabilities required by the Mem0 structure bundle."""


@dataclass(frozen=True, slots=True)
class Mem0Structures:
    """Keyed, temporal, and graph views used by the Mem0 service."""

    records: KeyedCollection
    timeline: TemporalCollection
    graph: PropertyGraph

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: Mem0StructureStore,
    ) -> Mem0Structures:
        """Build all Mem0 structural views around one bounded root."""

        if memory.root_id is None:
            raise ValueError("Mem0Structures requires a bounded MemoryHandle.")
        return cls(
            records=KeyedCollection(memory, store, collection=memory.root_id),
            timeline=TemporalCollection(memory, store, collection=memory.root_id),
            graph=PropertyGraph(memory),
        )


class Mem0Memory:
    """Small Mem0-inspired memory service over one bounded Genie handle."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: Mem0Structures,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create the application service.

        Args:
            memory: Bounded handle owning all records and mental models.
            structures: Keyed, temporal, and graph views over the same handle.
            clock: Optional UTC clock used to make tests deterministic.
        """

        self.memory = memory
        self.structures = structures
        self._clock = clock or (lambda: datetime.now(UTC))

    def add(
        self,
        text: str,
        *,
        scope: MemoryScope,
        kind: MemoryKind = "fact",
        entities: Sequence[str] = (),
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractedFactRecord:
        """Store an extracted-memory record and link records sharing entities.

        Exact duplicate text in the same session scope returns the existing
        record. This deterministic rule stands in for Mem0's model-driven
        extraction and conflict-resolution pipeline.

        Args:
            text: Extracted memory text.
            scope: User, agent, and run identifiers used for session scope.
            kind: Lamp-level `fact` or `experience` classification.
            entities: Entity names used by graph retrieval.
            occurred_at: Optional event time represented by the memory.
            metadata: Additional application metadata.

        Returns:
            New or existing extracted-memory record.
        """

        normalized_text = _required_text(text, field="text")
        normalized_kind = _validate_kind(kind)
        normalized_entities = _normalized_strings(entities, field="entities")
        normalized_occurred_at = (
            _normalize_timestamp(occurred_at, field="occurred_at")
            if occurred_at is not None
            else None
        )
        content_hash = _content_hash(normalized_text)
        for record in self._records(scope):
            if record.hash == content_hash and record.text == normalized_text:
                return record

        record_metadata = deepcopy(metadata or {})
        overlap = _RESERVED_METADATA.intersection(record_metadata)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"Metadata fields are managed by this lamp: {names}.")
        record_metadata.update(
            {
                "kind": normalized_kind,
                "entities": list(normalized_entities),
                "occurred_at": (
                    _format_timestamp(normalized_occurred_at)
                    if normalized_occurred_at is not None
                    else None
                ),
            }
        )
        now = _normalize_timestamp(self._clock(), field="clock")
        record_id = f"mem0_{uuid4().hex}"
        payload = {
            "id": record_id,
            "memory": normalized_text,
            **scope.as_dict(),
            "hash": content_hash,
            "metadata": record_metadata,
            "created_at": _format_timestamp(now),
            "updated_at": _format_timestamp(now),
        }
        block = self.memory.put(
            id=record_id,
            type=FACT_RECORD_TYPE,
            value=payload,
            metadata=_record_index(payload),
        )
        try:
            self.structures.records.bind(_record_address(scope, record_id), block)
            if normalized_occurred_at is not None:
                self.structures.timeline.add(
                    block,
                    occurred_at=normalized_occurred_at,
                )
        except Exception:
            self.memory.delete(block)
            raise
        stored = _record_from_block(block)
        self._link_shared_entities(stored)
        return stored

    def update(
        self,
        memory_id: str,
        *,
        scope: MemoryScope,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractedFactRecord:
        """Update text or application metadata while preserving identity.

        The classification, occurrence time, entities, scope, and creation time
        are stable in this compact example. Keeping entities stable also keeps
        existing entity links accurate.

        Args:
            memory_id: Extracted-memory record id.
            scope: Exact scope required to access the record.
            text: Optional replacement text.
            metadata: Optional application metadata to merge.

        Returns:
            Updated record with a new content hash and `updated_at` value.

        Raises:
            KeyError: If the record is absent from the exact scope.
            ValueError: If no update is supplied or it would create a duplicate.
        """

        if text is None and metadata is None:
            raise ValueError("An updated text or metadata object is required.")
        stored = self.get(memory_id, scope=scope)
        if stored is None:
            raise KeyError(f"Memory {memory_id!r} was not found in the bound scope.")

        new_text = stored.text if text is None else _required_text(text, field="text")
        new_hash = _content_hash(new_text)
        for candidate in self._records(scope):
            if candidate.id != stored.id and candidate.hash == new_hash and candidate.text == new_text:
                raise ValueError("The update would duplicate another memory in this scope.")

        merged_metadata = deepcopy(stored.metadata)
        if metadata is not None:
            overlap = _RESERVED_METADATA.intersection(metadata)
            if overlap:
                names = ", ".join(sorted(overlap))
                raise ValueError(f"Metadata fields are managed by this lamp: {names}.")
            merged_metadata.update(deepcopy(metadata))
        updated_at = _format_timestamp(_normalize_timestamp(self._clock(), field="clock"))
        block = self.memory.update(
            stored.block,
            value={
                "memory": new_text,
                "hash": new_hash,
                "metadata": merged_metadata,
                "updated_at": updated_at,
            },
            metadata={"hash": new_hash},
        )
        return _record_from_block(block)

    def get(
        self,
        memory_id: str,
        *,
        scope: MemoryScope,
    ) -> ExtractedFactRecord | None:
        """Get one fact record only when its exact scope matches."""

        block = self.structures.records.get(_record_address(scope, memory_id))
        return _record_from_block(block) if block is not None else None

    def search(
        self,
        query: str,
        *,
        scope: MemoryScope,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 5,
    ) -> list[SearchResult]:
        """Rank scoped records using deterministic lexical and entity overlap."""

        normalized_query = _required_text(query, field="query")
        allowed_kinds = _validated_kinds(kinds)
        normalized_limit = _validate_limit(limit)
        query_tokens = _tokens(normalized_query)
        ranked: list[SearchResult] = []
        for record in self._records(scope):
            if allowed_kinds and record.kind not in allowed_kinds:
                continue
            text_tokens = _tokens(record.text)
            entity_tokens = _tokens(" ".join(record.entities))
            text_overlap = query_tokens.intersection(text_tokens)
            entity_overlap = query_tokens.intersection(entity_tokens)
            if not text_overlap and not entity_overlap:
                continue
            score = len(text_overlap) / len(query_tokens)
            score += 0.25 * len(entity_overlap) / len(query_tokens)
            if normalized_query.casefold() in record.text.casefold():
                score += 0.25
            reasons: list[str] = []
            if text_overlap:
                reasons.append(f"text: {', '.join(sorted(text_overlap))}")
            if entity_overlap:
                reasons.append(f"entities: {', '.join(sorted(entity_overlap))}")
            ranked.append(SearchResult(record, round(score, 4), tuple(reasons)))
        ranked.sort(key=lambda result: (-result.score, result.memory.text, result.memory.id))
        return ranked[:normalized_limit]

    def search_temporal(
        self,
        start: datetime,
        end: datetime,
        *,
        scope: MemoryScope,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 20,
    ) -> list[ExtractedFactRecord]:
        """Return scoped memories whose event time is inside an inclusive range."""

        normalized_start = _normalize_timestamp(start, field="start")
        normalized_end = _normalize_timestamp(end, field="end")
        if normalized_start > normalized_end:
            raise ValueError("start must be earlier than or equal to end.")
        allowed_kinds = _validated_kinds(kinds)
        normalized_limit = _validate_limit(limit)
        try:
            exclusive_end = normalized_end + timedelta(microseconds=1)
        except OverflowError:
            exclusive_end = None
        matches = []
        for temporal in self.structures.timeline.events(
            start=normalized_start,
            stop=exclusive_end,
            limit=_SEARCH_LIMIT,
        ):
            if temporal.block.type != FACT_RECORD_TYPE:
                continue
            record = _record_from_block(temporal.block)
            occurred_at = temporal.entry.occurred_at
            if record.scope != scope or occurred_at is None or occurred_at > normalized_end:
                continue
            if allowed_kinds and record.kind not in allowed_kinds:
                continue
            matches.append(record)
        matches.sort(key=lambda record: (record.occurred_at, record.text, record.id))
        return matches[:normalized_limit]

    def search_graph(
        self,
        memory_id: str,
        *,
        scope: MemoryScope,
        max_depth: int = 1,
        limit: int = 20,
    ) -> list[GraphResult]:
        """Traverse application-owned links created from shared entities."""

        anchor = self.get(memory_id, scope=scope)
        if anchor is None:
            raise KeyError(f"Memory {memory_id!r} was not found in the bound scope.")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
            raise ValueError("max_depth must be an integer of at least 1.")
        normalized_limit = _validate_limit(limit)
        queue: deque[tuple[str, int]] = deque([(anchor.id, 0)])
        visited = {anchor.id}
        results: list[GraphResult] = []
        while queue and len(results) < normalized_limit:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            neighbors = self.structures.graph.neighbors(
                current_id,
                relation=ENTITY_LINK_RELATION,
                direction="both",
            )
            connected: list[
                tuple[ExtractedFactRecord, tuple[str, ...], GraphDirection]
            ] = []
            for neighbor_record in neighbors:
                link = neighbor_record.link
                neighbor_id = neighbor_record.block.id
                direction: GraphDirection = neighbor_record.direction
                if neighbor_id in visited:
                    continue
                neighbor = self.get(neighbor_id, scope=scope)
                if neighbor is None:
                    continue
                shared = tuple(cast(list[str], link.metadata.get("shared_entities", [])))
                connected.append((neighbor, shared, direction))
            connected.sort(key=lambda item: item[0].text.casefold())
            for neighbor, shared, direction in connected:
                if neighbor.id in visited:
                    continue
                visited.add(neighbor.id)
                next_depth = depth + 1
                results.append(GraphResult(neighbor, shared, direction, next_depth))
                if len(results) >= normalized_limit:
                    break
                queue.append((neighbor.id, next_depth))
        return results

    def create_mental_model(
        self,
        *,
        name: str,
        source_query: str,
        content: str,
        source_memory_ids: Sequence[str],
        scope: MemoryScope,
    ) -> MentalModel:
        """Store a separate synthesized document supported by fact records."""

        normalized_name = _required_text(name, field="name")
        normalized_query = _required_text(source_query, field="source_query")
        normalized_content = _required_text(content, field="content")
        normalized_sources = _normalized_strings(source_memory_ids, field="source_memory_ids")
        if not normalized_sources:
            raise ValueError("A mental model requires at least one source memory.")
        for memory_id in normalized_sources:
            if self.get(memory_id, scope=scope) is None:
                raise ValueError(f"Source memory {memory_id!r} is outside the bound scope.")
        if self.get_mental_model(normalized_name, scope=scope) is not None:
            raise ValueError(f"Mental model {normalized_name!r} already exists in this scope.")

        now = _format_timestamp(_normalize_timestamp(self._clock(), field="clock"))
        payload = {
            "name": normalized_name,
            "source_query": normalized_query,
            "content": normalized_content,
            "source_memory_ids": list(normalized_sources),
            "scope": scope.as_dict(),
            "created_at": now,
            "updated_at": now,
        }
        block = self.memory.put(
            type=MENTAL_MODEL_TYPE,
            value=payload,
            metadata={**scope.as_dict(), "name": normalized_name},
        )
        try:
            self.structures.records.bind(
                _mental_model_address(scope, normalized_name),
                block,
            )
        except Exception:
            self.memory.delete(block)
            raise
        return _mental_model_from_block(block)

    def get_mental_model(self, name: str, *, scope: MemoryScope) -> MentalModel | None:
        """Return one named mental model from the exact scope."""

        normalized_name = _required_text(name, field="name")
        block = self.structures.records.get(
            _mental_model_address(scope, normalized_name)
        )
        return _mental_model_from_block(block) if block is not None else None

    def _records(self, scope: MemoryScope) -> list[ExtractedFactRecord]:
        """Return all records from one exact session scope."""

        records = [
            _record_from_block(item.block)
            for item in self.structures.records.items(
                namespace=_scope_namespace(scope),
                limit=_SEARCH_LIMIT,
            )
            if item.block.type == FACT_RECORD_TYPE
        ]
        records.sort(key=lambda record: (record.created_at, record.text, record.id))
        return records

    def _link_shared_entities(self, record: ExtractedFactRecord) -> None:
        """Create one undirected-by-traversal link per shared record pair."""

        record_entities = {entity.casefold(): entity for entity in record.entities}
        if not record_entities:
            return
        for candidate in self._records(record.scope):
            if candidate.id == record.id:
                continue
            candidate_entities = {entity.casefold(): entity for entity in candidate.entities}
            shared = [
                candidate_entities[key]
                for key in sorted(record_entities.keys() & candidate_entities.keys())
            ]
            if shared:
                self.structures.graph.add_edge(
                    candidate.block,
                    record.block,
                    ENTITY_LINK_RELATION,
                    metadata={"shared_entities": shared},
                )


def _scope_namespace(scope: MemoryScope) -> tuple[str, ...]:
    """Return a stable keyed namespace for one exact Mem0 scope."""

    return (
        "records",
        f"user={scope.user_id or '-'}",
        f"agent={scope.agent_id or '-'}",
        f"run={scope.run_id or '-'}",
    )


def _record_address(scope: MemoryScope, memory_id: str) -> MemoryAddress:
    """Return the exact keyed address for one record id."""

    return MemoryAddress(_scope_namespace(scope), memory_id)


def _mental_model_address(scope: MemoryScope, name: str) -> MemoryAddress:
    """Return the scoped keyed address for one mental-model name."""

    return MemoryAddress((*_scope_namespace(scope), "mental_models"), name)


def _record_index(payload: dict[str, Any]) -> dict[str, Any]:
    """Return searchable block metadata for one record payload."""

    metadata = cast(dict[str, Any], payload["metadata"])
    return {
        "user_id": payload["user_id"],
        "agent_id": payload["agent_id"],
        "run_id": payload["run_id"],
        "hash": payload["hash"],
        "kind": metadata["kind"],
        "entities": deepcopy(metadata["entities"]),
        "occurred_at": metadata["occurred_at"],
    }


def _record_from_block(block: MemoryBlock) -> ExtractedFactRecord:
    """Decode and validate one extracted-memory block."""

    if block.type != FACT_RECORD_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a Mem0 fact record.")
    value = cast(dict[str, Any], block.value)
    expected = {
        "id",
        "memory",
        "user_id",
        "agent_id",
        "run_id",
        "hash",
        "metadata",
        "created_at",
        "updated_at",
    }
    if set(value) != expected or value.get("id") != block.id:
        raise TypeError(f"Block {block.id!r} does not have the expected memory-record shape.")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise TypeError(f"Block {block.id!r} has invalid record metadata.")
    _validate_kind(metadata.get("kind"))
    return ExtractedFactRecord(
        block=block,
        text=cast(str, value["memory"]),
        scope=MemoryScope(
            user_id=cast(str | None, value["user_id"]),
            agent_id=cast(str | None, value["agent_id"]),
            run_id=cast(str | None, value["run_id"]),
        ),
        hash=cast(str, value["hash"]),
        metadata=deepcopy(metadata),
        created_at=_parse_timestamp(cast(str, value["created_at"]), field="created_at"),
        updated_at=_parse_timestamp(cast(str, value["updated_at"]), field="updated_at"),
    )


def _mental_model_from_block(block: MemoryBlock) -> MentalModel:
    """Decode one separate mental-model document."""

    if block.type != MENTAL_MODEL_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a Mem0 mental model.")
    value = cast(dict[str, Any], block.value)
    scope_value = cast(dict[str, str | None], value["scope"])
    return MentalModel(
        block=block,
        name=cast(str, value["name"]),
        source_query=cast(str, value["source_query"]),
        content=cast(str, value["content"]),
        source_memory_ids=tuple(cast(list[str], value["source_memory_ids"])),
        scope=MemoryScope(**scope_value),
        created_at=_parse_timestamp(cast(str, value["created_at"]), field="created_at"),
        updated_at=_parse_timestamp(cast(str, value["updated_at"]), field="updated_at"),
    )


def _content_hash(text: str) -> str:
    """Return an MD5 content hash for compatibility-style deduplication."""

    # This is a content identifier, not a security primitive.
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _validate_kind(value: object) -> MemoryKind:
    """Validate the lamp-level record classification."""

    if value not in _KINDS:
        raise ValueError("kind must be 'fact' or 'experience'.")
    return cast(MemoryKind, value)


def _validated_kinds(values: Sequence[MemoryKind]) -> frozenset[MemoryKind]:
    """Validate a sequence of kind filters."""

    return frozenset(_validate_kind(value) for value in values)


def _normalized_strings(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    """Normalize a string sequence while preserving first occurrence order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required_text(value, field=field)
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return tuple(normalized)


def _required_text(value: object, *, field: str) -> str:
    """Return one stripped, non-empty string."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value.strip()


def _normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Normalize an aware timestamp to UTC."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse an ISO 8601 timestamp and normalize it to UTC."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    return _normalize_timestamp(parsed, field=field)


def _format_timestamp(value: datetime) -> str:
    """Format an aware timestamp in canonical UTC form."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tokens(value: str) -> set[str]:
    """Return normalized lexical tokens."""

    return set(_TOKEN_PATTERN.findall(value.casefold()))


def _validate_limit(value: int) -> int:
    """Validate a bounded retrieval limit."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("limit must be an integer of at least 1.")
    return min(value, _SEARCH_LIMIT)
