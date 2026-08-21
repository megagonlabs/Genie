"""Agent-callable tools for the Hindsight-inspired lamp.

The example agent is deterministic and independent of an LLM provider. Its
named tools use JSON-compatible arguments and results, so the same adapter can
be placed behind a model function-calling loop.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lamps.hindsight.memory_bank import (
    FactType,
    HindsightMemoryBank,
    MemoryState,
    MemoryUnit,
    MentalModel,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of one tool presented to the example agent."""

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Structured atomic memory the agent has selected for retention."""

    key: str
    text: str
    fact_type: FactType
    entities: tuple[str, ...] = ()
    occurred_dates: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryLinkCandidate:
    """Typed relationship between retained candidates identified by key."""

    source_key: str
    target_key: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MentalModelCandidate:
    """Synthesized document the agent intends to store separately."""

    name: str
    source_query: str
    content: str
    source_keys: tuple[str, ...]
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One inspectable tool call made by the example agent."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class HindsightTools:
    """Expose Hindsight-inspired bank operations as named agent tools."""

    def __init__(self, bank: HindsightMemoryBank) -> None:
        """Create tools backed by one bounded application memory bank.

        Args:
            bank: Hindsight-inspired application model.
        """

        self._bank = bank
        self.definitions = (
            ToolDefinition(
                name="retain",
                description=(
                    "Store one atomic world fact, agent experience, or derived observation "
                    "with entities, dates, tags, metadata, and provenance."
                ),
                parameters={
                    "text": "string",
                    "fact_type": "world | experience | observation",
                    "state": "valid | invalidated",
                    "entities": "list[string]",
                    "occurred_dates": "list[ISO 8601 timestamp]",
                    "metadata": "object",
                    "tags": "list[string]",
                    "provenance": "object",
                },
            ),
            ToolDefinition(
                name="link_memories",
                description="Connect two memory units with a temporal, semantic, or causal link.",
                parameters={
                    "source_id": "memory unit id",
                    "target_id": "memory unit id",
                    "relation": "hindsight.temporal | hindsight.semantic | hindsight.causal",
                    "metadata": "object",
                },
            ),
            ToolDefinition(
                name="create_mental_model",
                description="Store a synthesized document separately from atomic memory units.",
                parameters={
                    "name": "string",
                    "source_query": "string",
                    "content": "string",
                    "source_memory_ids": "list[memory unit id]",
                    "tags": "list[string]",
                },
            ),
            ToolDefinition(
                name="recall",
                description="Rank active memories using portable text, entity, type, and tag matching.",
                parameters={
                    "query": "string",
                    "entity": "optional string",
                    "fact_types": "optional list[world | experience | observation]",
                    "tags": "optional list[string]",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="recall_temporal",
                description="Retrieve active memories whose occurred dates fall inside a time range.",
                parameters={
                    "start": "ISO 8601 timestamp",
                    "end": "ISO 8601 timestamp",
                    "entity": "optional string",
                    "fact_types": "optional list[world | experience | observation]",
                },
            ),
            ToolDefinition(
                name="recall_graph",
                description="Traverse temporal, semantic, and causal links around a memory unit.",
                parameters={
                    "memory_id": "memory unit id",
                    "relations": "optional list[relation]",
                    "max_depth": "integer",
                },
            ),
            ToolDefinition(
                name="get_mental_model",
                description="Retrieve one named synthesized mental-model document.",
                parameters={"name": "string"},
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "retain": self._retain,
            "link_memories": self._link_memories,
            "create_mental_model": self._create_mental_model,
            "recall": self._recall,
            "recall_temporal": self._recall_temporal,
            "recall_graph": self._recall_graph,
            "get_mental_model": self._get_mental_model,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a named tool.

        Args:
            name: Tool name from `definitions`.
            arguments: JSON-compatible tool arguments.

        Returns:
            JSON-compatible tool result.

        Raises:
            ValueError: If the tool name is unknown.
            TypeError: If arguments do not match the selected tool.
        """

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown Hindsight tool {name!r}.")
        return handler(**arguments)

    def _retain(
        self,
        *,
        text: str,
        fact_type: FactType,
        state: MemoryState = "valid",
        entities: list[str] | None = None,
        occurred_dates: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement the `retain` tool."""

        unit = self._bank.retain(
            text,
            fact_type=fact_type,
            state=state,
            entities=entities or (),
            occurred_dates=[
                _parse_timestamp(value, field="occurred_dates")
                for value in (occurred_dates or ())
            ],
            metadata=metadata,
            tags=tags or (),
            provenance=provenance,
        )
        return _unit_result(unit)

    def _link_memories(
        self,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement the `link_memories` tool."""

        link = self._bank.link_units(source_id, target_id, relation, metadata=metadata)
        return {
            "link_id": link.id,
            "source_id": link.source_id,
            "target_id": link.target_id,
            "relation": link.relation,
            "metadata": deepcopy(link.metadata),
        }

    def _create_mental_model(
        self,
        *,
        name: str,
        source_query: str,
        content: str,
        source_memory_ids: list[str],
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Implement the `create_mental_model` tool."""

        model = self._bank.create_mental_model(
            name=name,
            source_query=source_query,
            content=content,
            source_memory_ids=source_memory_ids,
            tags=tags or (),
        )
        return _mental_model_result(model)

    def _recall(
        self,
        *,
        query: str,
        entity: str | None = None,
        fact_types: list[FactType] | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Implement the `recall` tool."""

        results = self._bank.recall(
            query,
            entity=entity,
            fact_types=fact_types or (),
            tags=tags or (),
            limit=limit,
        )
        return {
            "query": query,
            "results": [
                {
                    **_unit_result(result.unit),
                    "score": round(result.score, 3),
                    "reasons": list(result.reasons),
                }
                for result in results
            ],
        }

    def _recall_temporal(
        self,
        *,
        start: str,
        end: str,
        entity: str | None = None,
        fact_types: list[FactType] | None = None,
    ) -> dict[str, Any]:
        """Implement the `recall_temporal` tool."""

        units = self._bank.recall_temporal(
            _parse_timestamp(start, field="start"),
            _parse_timestamp(end, field="end"),
            entity=entity,
            fact_types=fact_types or (),
        )
        return {
            "start": start,
            "end": end,
            "results": [_unit_result(unit) for unit in units],
        }

    def _recall_graph(
        self,
        *,
        memory_id: str,
        relations: list[str] | None = None,
        max_depth: int = 1,
    ) -> dict[str, Any]:
        """Implement the `recall_graph` tool."""

        results = self._bank.recall_graph(
            memory_id,
            relations=relations or (),
            max_depth=max_depth,
        )
        return {
            "memory_id": memory_id,
            "results": [
                {
                    **_unit_result(result.unit),
                    "relation": result.relation,
                    "direction": result.direction,
                    "distance": result.distance,
                }
                for result in results
            ],
        }

    def _get_mental_model(self, *, name: str) -> dict[str, Any]:
        """Implement the `get_mental_model` tool."""

        model = self._bank.get_mental_model(name)
        if model is None:
            raise ValueError(f"Unknown mental model {name!r}.")
        return _mental_model_result(model)


class HindsightAgent:
    """Small agent that learns and recalls only through named tools."""

    def __init__(self, *, name: str, tools: HindsightTools) -> None:
        """Create an agent with no direct access to Genie or the memory bank.

        Args:
            name: Human-readable agent name.
            tools: Only memory interface the agent receives.
        """

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self._memory_ids: dict[str, str] = {}
        self.call_log: list[ToolCallRecord] = []

    def learn(
        self,
        *,
        memories: tuple[MemoryCandidate, ...],
        links: tuple[MemoryLinkCandidate, ...],
        mental_model: MentalModelCandidate,
    ) -> None:
        """Retain atomic units, connect them, and store a mental model.

        Args:
            memories: Ordered atomic units. Derived units may reference keys
                retained earlier in the sequence.
            links: Graph links expressed with candidate keys.
            mental_model: Separate synthesized document and its evidence keys.
        """

        for candidate in memories:
            if candidate.key in self._memory_ids:
                raise ValueError(f"Duplicate memory candidate key {candidate.key!r}.")
            provenance = deepcopy(candidate.provenance)
            if candidate.derived_from:
                provenance["source_memory_ids"] = [
                    self._require_memory_id(key) for key in candidate.derived_from
                ]
            result = self._call(
                "retain",
                {
                    "text": candidate.text,
                    "fact_type": candidate.fact_type,
                    "state": "valid",
                    "entities": list(candidate.entities),
                    "occurred_dates": list(candidate.occurred_dates),
                    "metadata": deepcopy(candidate.metadata),
                    "tags": list(candidate.tags),
                    "provenance": provenance,
                },
            )
            self._memory_ids[candidate.key] = result["id"]
        for link_candidate in links:
            self._call(
                "link_memories",
                {
                    "source_id": self._require_memory_id(link_candidate.source_key),
                    "target_id": self._require_memory_id(link_candidate.target_key),
                    "relation": link_candidate.relation,
                    "metadata": deepcopy(link_candidate.metadata),
                },
            )
        self._call(
            "create_mental_model",
            {
                "name": mental_model.name,
                "source_query": mental_model.source_query,
                "content": mental_model.content,
                "source_memory_ids": [
                    self._require_memory_id(key) for key in mental_model.source_keys
                ],
                "tags": list(mental_model.tags),
            },
        )

    def answer(
        self,
        question: str,
        *,
        query: str,
        entity: str,
        start: str,
        end: str,
        graph_anchor: str,
        mental_model_name: str,
    ) -> str:
        """Answer using lexical, temporal, graph, and mental-model retrieval.

        Args:
            question: User-facing question included in the report.
            query: Text query for general recall.
            entity: Entity used to constrain recall and temporal retrieval.
            start: Inclusive timeline start.
            end: Inclusive timeline end.
            graph_anchor: Candidate key at the center of graph traversal.
            mental_model_name: Synthesized document to retrieve first.

        Returns:
            Readable answer assembled from tool results.
        """

        model = self._call("get_mental_model", {"name": mental_model_name})
        recalled = self._call(
            "recall",
            {"query": query, "entity": entity, "limit": 3},
        )
        timeline = self._call(
            "recall_temporal",
            {
                "start": start,
                "end": end,
                "entity": entity,
                "fact_types": ["experience"],
            },
        )
        graph = self._call(
            "recall_graph",
            {
                "memory_id": self._require_memory_id(graph_anchor),
                "max_depth": 1,
            },
        )
        lines = [
            f"Question: {question}",
            f"Mental model ({model['name']}): {model['content']}",
            "Relevant atomic memories:",
            *[
                f"- [{result['fact_type']}] {result['text']}"
                for result in recalled["results"]
            ],
            f"Experience timeline for {entity}:",
            *[_timeline_line(result) for result in timeline["results"]],
            f"Graph context around {graph_anchor}:",
            *[_graph_line(result) for result in graph["results"]],
        ]
        return "\n".join(lines)

    def tool_usage(self) -> str:
        """Return a compact summary of all tool calls."""

        counts = Counter(record.name for record in self.call_log)
        return ", ".join(f"{name} x{count}" for name, count in counts.items())

    def memory_id(self, key: str) -> str:
        """Return the stored id assigned to a retained candidate key."""

        return self._require_memory_id(key)

    def _require_memory_id(self, key: str) -> str:
        """Resolve an agent-local candidate key or raise `ValueError`."""

        memory_id = self._memory_ids.get(key)
        if memory_id is None:
            raise ValueError(f"Unknown memory candidate key {key!r}.")
        return memory_id

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and retain an isolated trace record."""

        result = self._toolbox.call(name, arguments)
        self.call_log.append(
            ToolCallRecord(
                name=name,
                arguments=deepcopy(arguments),
                result=deepcopy(result),
            )
        )
        return result


def _unit_result(unit: MemoryUnit) -> dict[str, Any]:
    """Convert a resolved unit to a JSON-compatible tool result."""

    return {
        "id": unit.id,
        "text": unit.text,
        "fact_type": unit.fact_type,
        "state": unit.state,
        "entities": list(unit.entities),
        "occurred_dates": [_format_timestamp(value) for value in unit.occurred_dates],
        "metadata": deepcopy(unit.metadata),
        "tags": list(unit.tags),
        "provenance": deepcopy(unit.provenance),
    }


def _mental_model_result(model: MentalModel) -> dict[str, Any]:
    """Convert a mental model to a JSON-compatible tool result."""

    return {
        "id": model.id,
        "name": model.name,
        "source_query": model.source_query,
        "content": model.content,
        "source_memory_ids": list(model.source_memory_ids),
        "tags": list(model.tags),
    }


def _timeline_line(result: dict[str, Any]) -> str:
    """Render one temporal tool result."""

    occurred = ", ".join(_display_timestamp(value) for value in result["occurred_dates"])
    return f"- {occurred}: {result['text']}"


def _graph_line(result: dict[str, Any]) -> str:
    """Render one graph traversal result."""

    relation = result["relation"].removeprefix("hindsight.")
    return f"- {relation} ({result['direction']}): {result['text']}"


def _parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse one timezone-aware ISO 8601 tool timestamp."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    """Format a timestamp for JSON-compatible tool output."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _display_timestamp(value: str) -> str:
    """Render a tool timestamp as a date for the compact report."""

    return _parse_timestamp(value, field="occurred_dates").date().isoformat()
