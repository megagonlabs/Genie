"""Agent-callable tools for the Mem0-inspired lamp.

The deterministic agent stands in for an LLM function-calling loop. It sees
named JSON-compatible tools, while the underlying Genie handle remains private
to the application adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from lamps.mem0.memory_store import (
    ExtractedFactRecord,
    Mem0Memory,
    MemoryKind,
    MemoryScope,
    MentalModel,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of one function presented to the example agent."""

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Structured memory selected by the agent for storage."""

    key: str
    text: str
    kind: MemoryKind = "fact"
    entities: tuple[str, ...] = ()
    occurred_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryUpdateCandidate:
    """Update selected by the agent for a previously stored record."""

    key: str
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MentalModelCandidate:
    """Synthesis selected by the agent after retaining atomic records."""

    name: str
    source_query: str
    content: str
    source_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One inspectable tool call made by the deterministic agent."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class Mem0Tools:
    """Expose a scope-bound subset of Mem0-inspired operations to an agent."""

    def __init__(self, store: Mem0Memory, *, scope: MemoryScope) -> None:
        """Create tools that cannot escape one exact entity scope."""

        self._store = store
        self._scope = scope
        self.definitions = (
            ToolDefinition(
                name="add_memory",
                description="Add one extracted fact or experience and deduplicate exact text.",
                parameters={
                    "text": "string",
                    "kind": "fact | experience",
                    "entities": "list[string]",
                    "occurred_at": "optional ISO 8601 timestamp",
                    "metadata": "object",
                },
            ),
            ToolDefinition(
                name="update_memory",
                description="Replace record text or merge application metadata without changing its id.",
                parameters={
                    "memory_id": "memory id",
                    "text": "optional string",
                    "metadata": "optional object",
                },
            ),
            ToolDefinition(
                name="search_memories",
                description="Rank scoped facts and experiences by text and entity matches.",
                parameters={
                    "query": "string",
                    "kinds": "optional list[fact | experience]",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="search_temporal",
                description="Find memories whose event times fall in an inclusive interval.",
                parameters={
                    "start": "ISO 8601 timestamp",
                    "end": "ISO 8601 timestamp",
                    "kinds": "optional list[fact | experience]",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="search_graph",
                description="Traverse records linked because they mention a shared entity.",
                parameters={
                    "memory_id": "memory id",
                    "max_depth": "integer",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="create_mental_model",
                description="Store a separate synthesis supported by selected memory ids.",
                parameters={
                    "name": "string",
                    "source_query": "string",
                    "content": "string",
                    "source_memory_ids": "list[memory id]",
                },
            ),
            ToolDefinition(
                name="get_mental_model",
                description="Retrieve one named synthesis from the bound scope.",
                parameters={"name": "string"},
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_memory": self._add_memory,
            "update_memory": self._update_memory,
            "search_memories": self._search_memories,
            "search_temporal": self._search_temporal,
            "search_graph": self._search_graph,
            "create_mental_model": self._create_mental_model,
            "get_mental_model": self._get_mental_model,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one named tool with JSON-compatible arguments."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown Mem0 tool {name!r}.")
        return handler(**arguments)

    def _add_memory(
        self,
        *,
        text: str,
        kind: MemoryKind = "fact",
        entities: list[str] | None = None,
        occurred_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement `add_memory`."""

        record = self._store.add(
            text,
            scope=self._scope,
            kind=kind,
            entities=entities or (),
            occurred_at=(
                _parse_timestamp(occurred_at, field="occurred_at")
                if occurred_at is not None
                else None
            ),
            metadata=metadata,
        )
        return _record_result(record)

    def _update_memory(
        self,
        *,
        memory_id: str,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement `update_memory`."""

        return _record_result(
            self._store.update(
                memory_id,
                scope=self._scope,
                text=text,
                metadata=metadata,
            )
        )

    def _search_memories(
        self,
        *,
        query: str,
        kinds: list[MemoryKind] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Implement `search_memories`."""

        results = self._store.search(
            query,
            scope=self._scope,
            kinds=kinds or (),
            limit=limit,
        )
        return {
            "memories": [
                {
                    **_record_result(result.memory),
                    "score": result.score,
                    "reasons": list(result.reasons),
                }
                for result in results
            ]
        }

    def _search_temporal(
        self,
        *,
        start: str,
        end: str,
        kinds: list[MemoryKind] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Implement `search_temporal`."""

        memories = self._store.search_temporal(
            _parse_timestamp(start, field="start"),
            _parse_timestamp(end, field="end"),
            scope=self._scope,
            kinds=kinds or (),
            limit=limit,
        )
        return {"memories": [_record_result(memory) for memory in memories]}

    def _search_graph(
        self,
        *,
        memory_id: str,
        max_depth: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Implement `search_graph`."""

        results = self._store.search_graph(
            memory_id,
            scope=self._scope,
            max_depth=max_depth,
            limit=limit,
        )
        return {
            "memories": [
                {
                    **_record_result(result.memory),
                    "shared_entities": list(result.shared_entities),
                    "direction": result.direction,
                    "distance": result.distance,
                }
                for result in results
            ]
        }

    def _create_mental_model(
        self,
        *,
        name: str,
        source_query: str,
        content: str,
        source_memory_ids: list[str],
    ) -> dict[str, Any]:
        """Implement `create_mental_model`."""

        model = self._store.create_mental_model(
            name=name,
            source_query=source_query,
            content=content,
            source_memory_ids=source_memory_ids,
            scope=self._scope,
        )
        return _mental_model_result(model)

    def _get_mental_model(self, *, name: str) -> dict[str, Any]:
        """Implement `get_mental_model`."""

        model = self._store.get_mental_model(name, scope=self._scope)
        return {"mental_model": _mental_model_result(model) if model is not None else None}

    def __iter__(self) -> Iterator[ToolDefinition]:
        """Iterate over tool definitions for display or registration."""

        return iter(self.definitions)


class Mem0Agent:
    """Deterministic agent that learns and answers only through named tools."""

    def __init__(self, *, name: str, tools: Mem0Tools) -> None:
        """Create an agent with a tool adapter and no direct memory access."""

        self.name = name
        self.tools = tools
        self.tool_calls: list[ToolCallRecord] = []
        self._memory_ids: dict[str, str] = {}

    def learn(
        self,
        *,
        memories: tuple[MemoryCandidate, ...],
        updates: tuple[MemoryUpdateCandidate, ...] = (),
        mental_model: MentalModelCandidate | None = None,
    ) -> None:
        """Use write tools to retain, revise, link, and synthesize memories."""

        for candidate in memories:
            result = self._call(
                "add_memory",
                {
                    "text": candidate.text,
                    "kind": candidate.kind,
                    "entities": list(candidate.entities),
                    "occurred_at": candidate.occurred_at,
                    "metadata": deepcopy(candidate.metadata),
                },
            )
            self._memory_ids[candidate.key] = result["id"]
        for update_candidate in updates:
            memory_id = self._memory_ids.get(update_candidate.key)
            if memory_id is None:
                raise ValueError(f"Unknown memory key {update_candidate.key!r}.")
            arguments: dict[str, Any] = {
                "memory_id": memory_id,
                "text": update_candidate.text,
                "metadata": deepcopy(update_candidate.metadata) or None,
            }
            self._call("update_memory", arguments)
        if mental_model is not None:
            source_ids = [self._required_memory_id(key) for key in mental_model.source_keys]
            self._call(
                "create_mental_model",
                {
                    "name": mental_model.name,
                    "source_query": mental_model.source_query,
                    "content": mental_model.content,
                    "source_memory_ids": source_ids,
                },
            )

    def answer(
        self,
        question: str,
        *,
        query: str,
        start: str,
        end: str,
        graph_anchor: str,
        mental_model_name: str,
    ) -> str:
        """Use semantic, temporal, graph, and model tools to answer a question."""

        semantic = self._call(
            "search_memories",
            {"query": query, "kinds": ["fact", "experience"], "limit": 4},
        )["memories"]
        temporal = self._call(
            "search_temporal",
            {"start": start, "end": end, "kinds": ["experience"], "limit": 10},
        )["memories"]
        graph = self._call(
            "search_graph",
            {"memory_id": self._required_memory_id(graph_anchor), "max_depth": 1, "limit": 10},
        )["memories"]
        model = self._call("get_mental_model", {"name": mental_model_name})["mental_model"]

        preference = semantic[0]["memory"] if semantic else "No matching preference was found."
        timeline = _format_bullets(
            (memory["memory"] for memory in temporal),
            empty="No events found.",
        )
        connected = _format_bullets(
            (memory["memory"] for memory in graph),
            empty="No related memories.",
        )
        synthesis = model["content"] if model is not None else "No mental model is available."
        return (
            f"Question: {question}\n"
            f"Plan: {synthesis}\n"
            f"Best matching memory: {preference}\n"
            f"Prior experience timeline:\n{timeline}\n"
            f"Shared-entity context:\n{connected}"
        )

    def memory_id(self, key: str) -> str:
        """Return the stored id for a demo candidate key."""

        return self._required_memory_id(key)

    def tool_usage(self) -> dict[str, int]:
        """Return counts of inspectable calls made by the agent."""

        return dict(Counter(call.name for call in self.tool_calls))

    def _required_memory_id(self, key: str) -> str:
        memory_id = self._memory_ids.get(key)
        if memory_id is None:
            raise ValueError(f"Unknown memory key {key!r}.")
        return memory_id

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.tools.call(name, arguments)
        self.tool_calls.append(
            ToolCallRecord(name, deepcopy(arguments), deepcopy(result))
        )
        return result


def _record_result(record: ExtractedFactRecord) -> dict[str, Any]:
    """Return the JSON-compatible extracted-memory record."""

    return record.canonical_value()


def _format_bullets(values: Iterable[str], *, empty: str) -> str:
    """Format readable evidence lines for the agent answer."""

    items = list(values)
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def _mental_model_result(model: MentalModel) -> dict[str, Any]:
    """Return a JSON-compatible synthesized-document view."""

    return {
        "id": model.id,
        "name": model.name,
        "source_query": model.source_query,
        "content": model.content,
        "source_memory_ids": list(model.source_memory_ids),
        "created_at": _format_timestamp(model.created_at),
        "updated_at": _format_timestamp(model.updated_at),
    }


def _parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse an aware ISO 8601 timestamp."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed


def _format_timestamp(value: datetime) -> str:
    """Format a timestamp in UTC form."""

    return value.isoformat().replace("+00:00", "Z")
