"""Hindsight-inspired application model built from Genie primitives.

This module keeps memory taxonomy, graph meaning, and retrieval behavior in the
lamp. Genie provides blocks, links, scoped access, and portable search storage.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from typing import Any, Literal, Protocol, Sequence, cast

from genie import MemoryBlock, MemoryHandle, MemoryLink
from genie.backend import KeyedStore, SequenceStore, TemporalStore
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    PropertyGraph,
    TemporalCollection,
)

MEMORY_UNIT_TYPE = "hindsight.memory_unit"
MENTAL_MODEL_TYPE = "hindsight.mental_model"
OCCURRENCE_TYPE = "hindsight.occurrence"

TEMPORAL_RELATION = "hindsight.temporal"
SEMANTIC_RELATION = "hindsight.semantic"
CAUSAL_RELATION = "hindsight.causal"
OCCURRENCE_OF_RELATION = "hindsight.occurrence_of"
GRAPH_RELATIONS = frozenset({TEMPORAL_RELATION, SEMANTIC_RELATION, CAUSAL_RELATION})

FactType = Literal["world", "experience", "observation"]
MemoryState = Literal["valid", "invalidated"]
GraphDirection = Literal["out", "in"]

_FACT_TYPES = frozenset({"world", "experience", "observation"})
_MEMORY_STATES = frozenset({"valid", "invalidated"})
_SEARCH_LIMIT = 1_000
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class MemoryUnit:
    """Resolved view of one atomic Hindsight-inspired memory unit."""

    block: MemoryBlock
    text: str
    fact_type: FactType
    state: MemoryState
    entities: tuple[str, ...]
    occurred_dates: tuple[datetime, ...]
    metadata: dict[str, Any]
    tags: tuple[str, ...]
    provenance: dict[str, Any]

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class MentalModel:
    """Separate synthesized document derived from selected memory units."""

    block: MemoryBlock
    name: str
    source_query: str
    content: str
    source_memory_ids: tuple[str, ...]
    tags: tuple[str, ...]

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class RecallResult:
    """One ranked result from portable lexical recall."""

    unit: MemoryUnit
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphRecallResult:
    """A memory reached by traversing one or more typed graph links."""

    unit: MemoryUnit
    relation: str
    direction: GraphDirection
    distance: int


class HindsightStructureStore(KeyedStore, SequenceStore, TemporalStore, Protocol):
    """Backend capabilities required by the Hindsight structure bundle."""


@dataclass(frozen=True, slots=True)
class HindsightStructures:
    """Ordered, keyed, temporal, and graph views used by the memory bank."""

    units: EventLog
    names: KeyedCollection
    timeline: TemporalCollection
    graph: PropertyGraph

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: HindsightStructureStore,
    ) -> HindsightStructures:
        """Build all Hindsight structural views around one bounded root."""

        if memory.root_id is None:
            raise ValueError("HindsightStructures requires a bounded MemoryHandle.")
        return cls(
            units=EventLog(memory, store, log=memory.root_id),
            names=KeyedCollection(memory, store, collection=memory.root_id),
            timeline=TemporalCollection(memory, store, collection=memory.root_id),
            graph=PropertyGraph(memory),
        )


class HindsightMemoryBank:
    """Application-owned Hindsight-inspired memory bank.

    The bank is rooted in a bounded `MemoryHandle`. It defines the atomic unit
    schema, fact taxonomy, mental-model documents, graph relations, and simple
    retrieval strategies without adding those opinions to Genie core.
    """

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: HindsightStructures,
    ) -> None:
        """Create a bank over one bounded Genie memory handle.

        Args:
            memory: Handle whose root owns all units and mental models.
            structures: Ordered, keyed, temporal, and graph views.
        """

        self.memory = memory
        self.structures = structures

    def retain(
        self,
        text: str,
        *,
        fact_type: FactType,
        state: MemoryState = "valid",
        entities: Sequence[str] = (),
        occurred_dates: Sequence[datetime] = (),
        metadata: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        provenance: dict[str, Any] | None = None,
    ) -> MemoryUnit:
        """Store one compact application-owned memory unit.

        Args:
            text: Narrative fact or experience text.
            fact_type: `world`, `experience`, or derived `observation`.
            state: Curation state. Invalidated units remain stored but are
                excluded from recall by default.
            entities: Canonical entity names mentioned by the unit.
            occurred_dates: Effective dates described by the unit.
            metadata: Application-defined metadata retained inside the unit.
            tags: Application tags used for filtering.
            provenance: Source description or source-memory references.

        Returns:
            Stored atomic memory unit.

        Raises:
            ValueError: If text, taxonomy values, or timestamps are invalid.
        """

        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("Memory unit text cannot be empty.")
        normalized_type = _validate_fact_type(fact_type)
        normalized_state = _validate_state(state)
        normalized_entities = _normalized_strings(entities, field="entities")
        normalized_tags = _normalized_strings(tags, field="tags")
        normalized_dates = tuple(
            sorted(_normalize_timestamp(value, field="occurred_dates") for value in occurred_dates)
        )
        formatted_dates = [_format_timestamp(value) for value in normalized_dates]
        payload = {
            "text": normalized_text,
            "fact_type": normalized_type,
            "state": normalized_state,
            "entities": list(normalized_entities),
            "occurred_dates": formatted_dates,
            "metadata": deepcopy(metadata or {}),
            "tags": list(normalized_tags),
            "provenance": deepcopy(provenance or {}),
        }
        block = self.memory.put(
            type=MEMORY_UNIT_TYPE,
            value=payload,
            metadata={
                "fact_type": normalized_type,
                "state": normalized_state,
                "entities": list(normalized_entities),
                "occurred_dates": formatted_dates,
                "tags": list(normalized_tags),
            },
        )
        try:
            self.structures.names.bind(
                MemoryAddress(("units",), block.id),
                block,
            )
            self.structures.units.append(block)
            for ordinal, occurred_at in enumerate(normalized_dates):
                occurrence = self.memory.put(
                    parent=block,
                    type=OCCURRENCE_TYPE,
                    value={"memory_unit_id": block.id, "ordinal": ordinal},
                    metadata={"occurred_at": _format_timestamp(occurred_at)},
                )
                self.structures.timeline.add(
                    occurrence,
                    occurred_at=occurred_at,
                )
                self.structures.graph.add_edge(
                    occurrence,
                    block,
                    OCCURRENCE_OF_RELATION,
                )
        except Exception:
            self.memory.delete(block)
            raise
        return _memory_unit_from_block(block)

    def get_unit(self, unit: str | MemoryBlock | MemoryUnit) -> MemoryUnit | None:
        """Return one atomic memory unit when it exists.

        Args:
            unit: Unit, block, or block id.

        Returns:
            Resolved memory unit, or `None` when the id is missing.

        Raises:
            TypeError: If the block is not an atomic memory unit.
        """

        block = self.structures.names.get(
            MemoryAddress(("units",), _unit_id(unit))
        )
        return _memory_unit_from_block(block) if block is not None else None

    def set_state(
        self,
        unit: str | MemoryBlock | MemoryUnit,
        state: MemoryState,
    ) -> MemoryUnit:
        """Set an atomic unit's curation state.

        Args:
            unit: Existing atomic memory unit.
            state: `valid` or `invalidated`.

        Returns:
            Updated memory unit.
        """

        stored = self._require_unit(unit)
        normalized_state = _validate_state(state)
        updated = self.memory.update(
            stored.block,
            value={"state": normalized_state},
            metadata={"state": normalized_state},
        )
        return _memory_unit_from_block(updated)

    def link_units(
        self,
        source: str | MemoryBlock | MemoryUnit,
        target: str | MemoryBlock | MemoryUnit,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Connect two units with a temporal, semantic, or causal link.

        Args:
            source: Unit where the directed link starts.
            target: Unit where the directed link ends.
            relation: One of the lamp's graph relation constants.
            metadata: Optional relationship properties.

        Returns:
            Stored Genie link.

        Raises:
            ValueError: If the relation is not supported by this lamp.
        """

        if relation not in GRAPH_RELATIONS:
            raise ValueError(f"Unsupported Hindsight graph relation {relation!r}.")
        source_unit = self._require_unit(source)
        target_unit = self._require_unit(target)
        return self.structures.graph.add_edge(
            source_unit.block,
            target_unit.block,
            relation,
            metadata=deepcopy(metadata or {}),
        )

    def create_mental_model(
        self,
        *,
        name: str,
        source_query: str,
        content: str,
        source_memory_ids: Sequence[str],
        tags: Sequence[str] = (),
    ) -> MentalModel:
        """Store a synthesized document separately from atomic units.

        Args:
            name: Stable human-readable model name.
            source_query: Question the document summarizes.
            content: Synthesized answer or guidance.
            source_memory_ids: Atomic units used as evidence.
            tags: Application tags for the document.

        Returns:
            Stored mental model.

        Raises:
            ValueError: If required text is empty, evidence is missing, or the
                name already exists.
        """

        normalized_name = _required_text(name, field="name")
        normalized_query = _required_text(source_query, field="source_query")
        normalized_content = _required_text(content, field="content")
        normalized_tags = _normalized_strings(tags, field="tags")
        source_ids = tuple(dict.fromkeys(source_memory_ids))
        if not source_ids:
            raise ValueError("Mental models require at least one source memory.")
        for source_id in source_ids:
            self._require_unit(source_id)
        if self.get_mental_model(normalized_name) is not None:
            raise ValueError(f"Mental model {normalized_name!r} already exists.")
        block = self.memory.put(
            type=MENTAL_MODEL_TYPE,
            value={
                "name": normalized_name,
                "source_query": normalized_query,
                "content": normalized_content,
                "source_memory_ids": list(source_ids),
                "tags": list(normalized_tags),
            },
            metadata={"name": normalized_name, "tags": list(normalized_tags)},
        )
        try:
            self.structures.names.bind(
                MemoryAddress(("mental_models",), normalized_name),
                block,
            )
        except Exception:
            self.memory.delete(block)
            raise
        return _mental_model_from_block(block)

    def get_mental_model(self, name: str) -> MentalModel | None:
        """Find a mental model by its human-readable name.

        Args:
            name: Exact model name.

        Returns:
            Mental model, or `None` when no model has that name.
        """

        block = self.structures.names.get(
            MemoryAddress(("mental_models",), name)
        )
        return _mental_model_from_block(block) if block is not None else None

    def recall(
        self,
        query: str,
        *,
        entity: str | None = None,
        fact_types: Sequence[FactType] = (),
        tags: Sequence[str] = (),
        limit: int = 5,
        include_invalidated: bool = False,
    ) -> list[RecallResult]:
        """Recall units with deterministic lexical, entity, and tag scoring.

        This is a small portable stand-in for Hindsight's semantic and keyword
        retrieval. It intentionally does not claim embedding similarity.

        Args:
            query: Natural-language query whose tokens are matched.
            entity: Optional exact entity filter.
            fact_types: Optional memory-unit type filter.
            tags: Tags that every returned unit must contain.
            limit: Maximum number of results.
            include_invalidated: Whether invalidated units remain eligible.

        Returns:
            Ranked recall results.
        """

        normalized_query = _required_text(query, field="query")
        if limit < 0:
            raise ValueError("limit must be non-negative.")
        selected_types = {_validate_fact_type(value) for value in fact_types}
        selected_tags = set(_normalized_strings(tags, field="tags"))
        normalized_entity = entity.casefold() if entity is not None else None
        query_tokens = _tokens(normalized_query)
        if not query_tokens:
            raise ValueError("query must contain at least one letter or number.")
        results: list[RecallResult] = []
        for unit in self._all_units():
            if not include_invalidated and unit.state != "valid":
                continue
            if selected_types and unit.fact_type not in selected_types:
                continue
            if selected_tags and not selected_tags.issubset(unit.tags):
                continue
            if normalized_entity is not None and normalized_entity not in {
                value.casefold() for value in unit.entities
            }:
                continue
            haystack = " ".join((unit.text, *unit.entities, *unit.tags))
            overlap = query_tokens.intersection(_tokens(haystack))
            exact_phrase = normalized_query.casefold() in haystack.casefold()
            if not overlap and not exact_phrase:
                continue
            score = len(overlap) / len(query_tokens)
            reasons = ["token overlap"]
            if exact_phrase:
                score += 1.0
                reasons.append("exact phrase")
            if normalized_entity is not None:
                score += 0.25
                reasons.append("entity match")
            if selected_tags:
                score += 0.1
                reasons.append("tag match")
            results.append(RecallResult(unit=unit, score=score, reasons=tuple(reasons)))
        results.sort(key=lambda result: (-result.score, result.unit.text.casefold()))
        return results[:limit]

    def recall_temporal(
        self,
        start: datetime,
        end: datetime,
        *,
        entity: str | None = None,
        fact_types: Sequence[FactType] = (),
    ) -> list[MemoryUnit]:
        """Return active units with an occurred date inside an inclusive range.

        Args:
            start: Inclusive range start.
            end: Inclusive range end.
            entity: Optional exact entity filter.
            fact_types: Optional memory-unit type filter.

        Returns:
            Matching units ordered by their first in-range date.

        Raises:
            ValueError: If timestamps are naive or the range is reversed.
        """

        normalized_start = _normalize_timestamp(start, field="start")
        normalized_end = _normalize_timestamp(end, field="end")
        if normalized_end < normalized_start:
            raise ValueError("Temporal recall end must not be earlier than start.")
        selected_types = {_validate_fact_type(value) for value in fact_types}
        normalized_entity = entity.casefold() if entity is not None else None
        try:
            exclusive_end = normalized_end + timedelta(microseconds=1)
        except OverflowError:
            exclusive_end = None
        selected_by_id: dict[str, tuple[datetime, MemoryUnit]] = {}
        for temporal in self.structures.timeline.events(
            start=normalized_start,
            stop=exclusive_end,
            limit=_SEARCH_LIMIT,
        ):
            if temporal.block.type != OCCURRENCE_TYPE:
                continue
            occurred_at = temporal.entry.occurred_at
            if occurred_at is None or occurred_at > normalized_end:
                continue
            neighbors = self.structures.graph.neighbors(
                temporal.block,
                relation=OCCURRENCE_OF_RELATION,
            )
            if len(neighbors) != 1:
                raise ValueError(
                    f"Occurrence {temporal.block.id!r} must reference one memory unit."
                )
            unit = self.get_unit(neighbors[0].block)
            if unit is None:
                raise ValueError(
                    f"Occurrence {temporal.block.id!r} references a missing memory unit."
                )
            if unit.state != "valid":
                continue
            if selected_types and unit.fact_type not in selected_types:
                continue
            if normalized_entity is not None and normalized_entity not in {
                value.casefold() for value in unit.entities
            }:
                continue
            previous = selected_by_id.get(unit.id)
            if previous is None or occurred_at < previous[0]:
                selected_by_id[unit.id] = (occurred_at, unit)
        selected = list(selected_by_id.values())
        selected.sort(key=lambda item: (item[0], item[1].text.casefold()))
        return [unit for _, unit in selected]

    def recall_graph(
        self,
        start: str | MemoryBlock | MemoryUnit,
        *,
        relations: Sequence[str] = (),
        max_depth: int = 1,
    ) -> list[GraphRecallResult]:
        """Traverse temporal, semantic, and causal links around a unit.

        Args:
            start: Unit at the center of graph retrieval.
            relations: Optional subset of graph relations to follow.
            max_depth: Maximum number of link hops.

        Returns:
            Connected active units in breadth-first order.

        Raises:
            ValueError: If a relation or depth is invalid.
        """

        if max_depth < 1:
            raise ValueError("max_depth must be at least 1.")
        selected_relations = set(relations or GRAPH_RELATIONS)
        unsupported = selected_relations.difference(GRAPH_RELATIONS)
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"Unsupported Hindsight graph relations: {names}.")
        start_unit = self._require_unit(start)
        queue: deque[tuple[str, int]] = deque([(start_unit.id, 0)])
        visited = {start_unit.id}
        results: list[GraphRecallResult] = []
        while queue:
            current_id, distance = queue.popleft()
            if distance >= max_depth:
                continue
            neighbors = sorted(
                self.structures.graph.neighbors(current_id, direction="both"),
                key=lambda neighbor: (neighbor.link.relation, neighbor.link.id),
            )
            for neighbor_record in neighbors:
                link = neighbor_record.link
                if link.relation not in selected_relations:
                    continue
                neighbor_id = neighbor_record.block.id
                direction: GraphDirection = neighbor_record.direction
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)
                neighbor = self.get_unit(neighbor_id)
                if neighbor is None or neighbor.state != "valid":
                    continue
                next_distance = distance + 1
                results.append(
                    GraphRecallResult(
                        unit=neighbor,
                        relation=link.relation,
                        direction=direction,
                        distance=next_distance,
                    )
                )
                queue.append((neighbor.id, next_distance))
        return results

    def _all_units(self) -> list[MemoryUnit]:
        """Return every readable atomic unit in insertion order."""

        return [
            _memory_unit_from_block(item.block)
            for item in self.structures.units.read(limit=_SEARCH_LIMIT)
        ]

    def _require_unit(self, unit: str | MemoryBlock | MemoryUnit) -> MemoryUnit:
        """Return an existing memory unit or raise `ValueError`."""

        stored = self.get_unit(unit)
        if stored is None:
            raise ValueError(f"Unknown memory unit {_unit_id(unit)!r}.")
        return stored


def _memory_unit_from_block(block: MemoryBlock) -> MemoryUnit:
    """Decode and validate an atomic unit block."""

    if block.type != MEMORY_UNIT_TYPE:
        raise TypeError(f"Expected block type {MEMORY_UNIT_TYPE!r}, got {block.type!r}.")
    payload = block.value
    if not isinstance(payload, dict):
        raise TypeError("Memory unit value must be a dictionary.")
    return MemoryUnit(
        block=block,
        text=_payload_string(payload, "text"),
        fact_type=_validate_fact_type(_payload_string(payload, "fact_type")),
        state=_validate_state(_payload_string(payload, "state")),
        entities=_payload_strings(payload, "entities"),
        occurred_dates=tuple(
            _parse_timestamp(value, field="occurred_dates")
            for value in _payload_strings(payload, "occurred_dates")
        ),
        metadata=_payload_dict(payload, "metadata"),
        tags=_payload_strings(payload, "tags"),
        provenance=_payload_dict(payload, "provenance"),
    )


def _mental_model_from_block(block: MemoryBlock) -> MentalModel:
    """Decode and validate a mental-model block."""

    if block.type != MENTAL_MODEL_TYPE:
        raise TypeError(f"Expected block type {MENTAL_MODEL_TYPE!r}, got {block.type!r}.")
    payload = block.value
    if not isinstance(payload, dict):
        raise TypeError("Mental model value must be a dictionary.")
    return MentalModel(
        block=block,
        name=_payload_string(payload, "name"),
        source_query=_payload_string(payload, "source_query"),
        content=_payload_string(payload, "content"),
        source_memory_ids=_payload_strings(payload, "source_memory_ids"),
        tags=_payload_strings(payload, "tags"),
    )


def _unit_id(unit: str | MemoryBlock | MemoryUnit) -> str:
    """Return a memory-unit block id."""

    if isinstance(unit, MemoryUnit):
        return unit.id
    if isinstance(unit, MemoryBlock):
        return unit.id
    return unit


def _validate_fact_type(value: str) -> FactType:
    """Validate and narrow an atomic fact type."""

    if value not in _FACT_TYPES:
        raise ValueError("fact_type must be 'world', 'experience', or 'observation'.")
    return cast(FactType, value)


def _validate_state(value: str) -> MemoryState:
    """Validate and narrow a memory state."""

    if value not in _MEMORY_STATES:
        raise ValueError("state must be 'valid' or 'invalidated'.")
    return cast(MemoryState, value)


def _normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Normalize a timezone-aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse a timezone-aware ISO 8601 timestamp."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must contain ISO 8601 timestamps.") from error
    return _normalize_timestamp(parsed, field=field)


def _format_timestamp(value: datetime) -> str:
    """Format a UTC timestamp for storage and tool output."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_text(value: str, *, field: str) -> str:
    """Strip required text or raise `ValueError`."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty.")
    return normalized


def _normalized_strings(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    """Return unique non-empty strings while preserving input order."""

    normalized: list[str] = []
    for value in values:
        item = _required_text(value, field=field)
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _tokens(value: str) -> set[str]:
    """Return normalized alphanumeric tokens for portable lexical recall."""

    return set(_TOKEN_PATTERN.findall(value.casefold()))


def _payload_string(payload: dict[str, Any], field: str) -> str:
    """Read one required string field from a stored payload."""

    value = payload.get(field)
    if not isinstance(value, str):
        raise TypeError(f"Stored field {field!r} must be a string.")
    return value


def _payload_strings(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    """Read one list-of-strings field from a stored payload."""

    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"Stored field {field!r} must be a list of strings.")
    return tuple(value)


def _payload_dict(payload: dict[str, Any], field: str) -> dict[str, Any]:
    """Read one dictionary field from a stored payload."""

    value = payload.get(field)
    if not isinstance(value, dict):
        raise TypeError(f"Stored field {field!r} must be a dictionary.")
    return deepcopy(value)
