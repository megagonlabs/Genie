"""Agent-callable tools for the LangMem-inspired lamp."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from lamps.langmem.memory_store import LangMemItem, LangMemStore, Namespace


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of one tool presented to the example agent."""

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Schema-shaped namespaced value selected for storage by the agent."""

    collection: str
    key: str
    value: dict[str, Any]
    occurred_at: str | None = None
    entities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryLinkCandidate:
    """Graph relation selected between two namespaced items."""

    source_collection: str
    source_key: str
    target_collection: str
    target_key: str
    relation: str


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One inspectable tool call made by the deterministic agent."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class LangMemTools:
    """Expose namespace-bound LangMem-inspired operations to an agent."""

    def __init__(self, store: LangMemStore, *, base_namespace: Namespace) -> None:
        """Create a tool set permanently bound below one namespace prefix."""

        if not base_namespace:
            raise ValueError("A base namespace is required.")
        self._store = store
        self._base_namespace = tuple(base_namespace)
        self.definitions = (
            ToolDefinition(
                name="manage_memory",
                description="Create, update, or delete a structured value under a bound namespace.",
                parameters={
                    "action": "create | update | delete",
                    "collection": "string",
                    "key": "string",
                    "value": "schema-shaped object; omitted for delete",
                    "occurred_at": "optional ISO 8601 timestamp",
                    "entities": "list[string]",
                },
            ),
            ToolDefinition(
                name="get_memory",
                description="Retrieve one item directly by collection and key.",
                parameters={"collection": "string", "key": "string"},
            ),
            ToolDefinition(
                name="search_memory",
                description="Search structured values below the bound namespace.",
                parameters={
                    "query": "string",
                    "collections": "optional list[string]",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="search_temporal",
                description="Retrieve dated items within an inclusive event-time interval.",
                parameters={
                    "start": "ISO 8601 timestamp",
                    "end": "ISO 8601 timestamp",
                    "collections": "optional list[string]",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="link_memories",
                description="Connect two namespace/key items with an application relation.",
                parameters={
                    "source_collection": "string",
                    "source_key": "string",
                    "target_collection": "string",
                    "target_key": "string",
                    "relation": "string",
                },
            ),
            ToolDefinition(
                name="search_graph",
                description="Traverse application relations around one namespace/key item.",
                parameters={
                    "collection": "string",
                    "key": "string",
                    "max_depth": "integer",
                    "limit": "integer",
                },
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "manage_memory": self._manage_memory,
            "get_memory": self._get_memory,
            "search_memory": self._search_memory,
            "search_temporal": self._search_temporal,
            "link_memories": self._link_memories,
            "search_graph": self._search_graph,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one named tool with JSON-compatible arguments."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown LangMem tool {name!r}.")
        return handler(**arguments)

    def _manage_memory(
        self,
        *,
        action: Literal["create", "update", "delete"],
        collection: str,
        key: str,
        value: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        entities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Implement `manage_memory`."""

        namespace = self._namespace(collection)
        timestamp = (
            _parse_timestamp(occurred_at, field="occurred_at")
            if occurred_at is not None
            else None
        )
        if action == "create":
            if value is None:
                raise ValueError("value is required when action is 'create'.")
            item = self._store.create(
                namespace,
                key,
                value,
                occurred_at=timestamp,
                entities=entities or (),
            )
        elif action == "update":
            if value is None:
                raise ValueError("value is required when action is 'update'.")
            item = self._store.update(
                namespace,
                key,
                value,
                occurred_at=timestamp,
                entities=entities,
            )
        elif action == "delete":
            if value is not None or occurred_at is not None or entities is not None:
                raise ValueError("delete accepts only collection and key.")
            item = self._store.delete(namespace, key)
        else:
            raise ValueError("action must be 'create', 'update', or 'delete'.")
        return {"action": action, "item": _item_result(item)}

    def _get_memory(self, *, collection: str, key: str) -> dict[str, Any]:
        """Implement `get_memory`."""

        item = self._store.get(self._namespace(collection), key)
        return {"item": _item_result(item) if item is not None else None}

    def _search_memory(
        self,
        *,
        query: str,
        collections: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Implement `search_memory`."""

        results = self._store.search(
            self._base_namespace,
            query,
            collections=collections or (),
            limit=limit,
        )
        return {
            "results": [
                {
                    "item": _item_result(result.item),
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
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Implement `search_temporal`."""

        items = self._store.search_temporal(
            self._base_namespace,
            _parse_timestamp(start, field="start"),
            _parse_timestamp(end, field="end"),
            collections=collections or (),
            limit=limit,
        )
        return {"items": [_item_result(item) for item in items]}

    def _link_memories(
        self,
        *,
        source_collection: str,
        source_key: str,
        target_collection: str,
        target_key: str,
        relation: str,
    ) -> dict[str, Any]:
        """Implement `link_memories`."""

        link = self._store.link(
            self._namespace(source_collection),
            source_key,
            self._namespace(target_collection),
            target_key,
            relation=relation,
        )
        return {
            "link_id": link.id,
            "relation": relation,
            "source": {"collection": source_collection, "key": source_key},
            "target": {"collection": target_collection, "key": target_key},
        }

    def _search_graph(
        self,
        *,
        collection: str,
        key: str,
        max_depth: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Implement `search_graph`."""

        results = self._store.search_graph(
            self._base_namespace,
            self._namespace(collection),
            key,
            max_depth=max_depth,
            limit=limit,
        )
        return {
            "results": [
                {
                    "item": _item_result(result.item),
                    "relation": result.relation,
                    "direction": result.direction,
                    "distance": result.distance,
                }
                for result in results
            ]
        }

    def _namespace(self, collection: str) -> Namespace:
        normalized = collection.strip()
        if not normalized or "/" in normalized:
            raise ValueError("collection must be one non-empty namespace segment.")
        return (*self._base_namespace, normalized)


class LangMemAgent:
    """Deterministic agent that reads and writes memory only through tools."""

    def __init__(self, *, name: str, tools: LangMemTools) -> None:
        """Create an agent with tool definitions and a private adapter."""

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self.call_log: list[ToolCallRecord] = []

    def learn(
        self,
        *,
        memories: tuple[MemoryCandidate, ...],
        links: tuple[MemoryLinkCandidate, ...],
    ) -> None:
        """Use tools to create schema-shaped items and graph relations."""

        for candidate in memories:
            self._call(
                "manage_memory",
                {
                    "action": "create",
                    "collection": candidate.collection,
                    "key": candidate.key,
                    "value": deepcopy(candidate.value),
                    "occurred_at": candidate.occurred_at,
                    "entities": list(candidate.entities),
                },
            )
        for link_candidate in links:
            self._call(
                "link_memories",
                {
                    "source_collection": link_candidate.source_collection,
                    "source_key": link_candidate.source_key,
                    "target_collection": link_candidate.target_collection,
                    "target_key": link_candidate.target_key,
                    "relation": link_candidate.relation,
                },
            )

    def answer(
        self,
        question: str,
        *,
        query: str,
        start: str,
        end: str,
        graph_collection: str,
        graph_key: str,
        profile_collection: str,
        profile_key: str,
    ) -> str:
        """Combine direct, lexical, temporal, and graph retrieval."""

        model = self._call(
            "get_memory",
            {"collection": profile_collection, "key": profile_key},
        )["item"]
        recalled = self._call(
            "search_memory",
            {
                "query": query,
                "collections": ["semantic", "episodes"],
                "limit": 4,
            },
        )["results"]
        timeline = self._call(
            "search_temporal",
            {
                "start": start,
                "end": end,
                "collections": ["episodes"],
                "limit": 10,
            },
        )["items"]
        graph = self._call(
            "search_graph",
            {
                "collection": graph_collection,
                "key": graph_key,
                "max_depth": 2,
                "limit": 10,
            },
        )["results"]

        model_summary = (
            model["value"]["summary"] if model is not None else "No semantic profile found."
        )
        relevant = "\n".join(
            f"- {_display_item(result['item'])}" for result in recalled
        ) or "- No matching memories."
        dated = "\n".join(
            f"- {_display_item(item)}" for item in timeline
        ) or "- No dated experiences."
        connected = "\n".join(
            f"- {result['relation']} ({result['direction']}, depth {result['distance']}): "
            f"{_display_item(result['item'])}"
            for result in graph
        ) or "- No related memories."
        return (
            f"Question: {question}\n"
            f"Semantic profile: {model_summary}\n"
            f"Relevant memories:\n{relevant}\n"
            f"Experience timeline:\n{dated}\n"
            f"Graph context:\n{connected}"
        )

    def tool_usage(self) -> dict[str, int]:
        """Return usage counts in first-call order."""

        return dict(Counter(call.name for call in self.call_log))

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._toolbox.call(name, arguments)
        self.call_log.append(ToolCallRecord(name, deepcopy(arguments), deepcopy(result)))
        return result


def _item_result(item: LangMemItem) -> dict[str, Any]:
    return item.canonical_value()


def _display_item(item: dict[str, Any]) -> str:
    value = item["value"]
    if all(field in value for field in ("subject", "predicate", "object")):
        return f"{value['subject']} {value['predicate']} {value['object']} ({value['context']})"
    if all(field in value for field in ("observation", "result")):
        return f"{value['observation']} Result: {value['result']}"
    if all(field in value for field in ("name", "summary")):
        return f"Semantic profile {value['name']}: {value['summary']}"
    return str(value)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed
