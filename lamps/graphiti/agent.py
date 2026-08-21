"""A small tool interface and deterministic agent for the Graphiti lamp.

The agent in this module is intentionally independent of an LLM provider or
agent framework. It receives named tools with JSON-compatible arguments and
results. An application can present the same tools to an LLM function-calling
loop without changing the temporal graph model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from genie import MemoryBlock
from lamps.graphiti.temporal_graph import ENTITY_TYPE, TemporalFact, TemporalKnowledgeGraph


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of a tool presented to an agent.

    Attributes:
        name: Stable tool name used for calls.
        description: Plain-language guidance for the agent.
        parameters: Mapping of argument names to short type descriptions.
    """

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """Structured fact an agent has selected from a source episode."""

    subject: str
    subject_type: str
    relation: str
    object: str
    object_type: str
    valid_from: str
    valid_to: str | None = None
    exclusive: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    """Source material and the facts an agent intends to remember."""

    content: str
    source: str
    observed_at: str
    facts: tuple[FactCandidate, ...]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call made by the example agent."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class TemporalGraphTools:
    """Expose temporal knowledge graph operations as agent-callable tools."""

    def __init__(self, graph: TemporalKnowledgeGraph) -> None:
        """Create tools backed by one bounded temporal knowledge graph.

        Args:
            graph: Application model that owns entity, episode, and fact logic.
        """

        self._graph = graph
        self.definitions = (
            ToolDefinition(
                name="add_episode",
                description="Store raw source material for later provenance.",
                parameters={
                    "content": "string",
                    "source": "string",
                    "observed_at": "ISO 8601 timestamp",
                },
            ),
            ToolDefinition(
                name="add_fact",
                description=("Store a temporal subject-relation-object fact and link it " "to its source episode. Entities are reused by name and type."),
                parameters={
                    "subject": "string",
                    "subject_type": "string",
                    "relation": "string",
                    "object": "string",
                    "object_type": "string",
                    "episode_id": "string",
                    "valid_from": "ISO 8601 timestamp",
                    "valid_to": "ISO 8601 timestamp or null",
                    "exclusive": "boolean",
                },
            ),
            ToolDefinition(
                name="query_entity",
                description="Return facts about an entity that are valid at a given time.",
                parameters={
                    "name": "string",
                    "as_of": "ISO 8601 timestamp",
                    "relation": "optional string",
                },
            ),
            ToolDefinition(
                name="query_neighborhood",
                description="Return incoming and outgoing facts around an entity at a given time.",
                parameters={"name": "string", "as_of": "ISO 8601 timestamp"},
            ),
            ToolDefinition(
                name="get_history",
                description="Return validity periods and provenance for an entity relationship.",
                parameters={"name": "string", "relation": "string"},
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_episode": self._add_episode,
            "add_fact": self._add_fact,
            "query_entity": self._query_entity,
            "query_neighborhood": self._query_neighborhood,
            "get_history": self._get_history,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool by name with JSON-compatible arguments.

        Args:
            name: Tool name from ``definitions``.
            arguments: Tool arguments.

        Returns:
            JSON-compatible tool result.

        Raises:
            ValueError: If the tool name is unknown.
            TypeError: If arguments do not match the selected tool.
        """

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown temporal graph tool {name!r}.")
        return handler(**arguments)

    def _add_episode(
        self,
        *,
        content: str,
        source: str,
        observed_at: str,
    ) -> dict[str, Any]:
        """Implement the ``add_episode`` tool."""

        episode = self._graph.add_episode(
            content,
            source=source,
            observed_at=_parse_timestamp(observed_at),
        )
        return {"episode_id": episode.id, "source": source}

    def _add_fact(
        self,
        *,
        subject: str,
        subject_type: str,
        relation: str,
        object: str,
        object_type: str,
        episode_id: str,
        valid_from: str,
        valid_to: str | None = None,
        exclusive: bool = False,
    ) -> dict[str, Any]:
        """Implement the ``add_fact`` tool."""

        subject_block = self._get_or_create_entity(subject, subject_type)
        object_block = self._get_or_create_entity(object, object_type)
        fact = self._graph.assert_fact(
            subject_block,
            relation,
            object_block,
            episode=episode_id,
            valid_from=_parse_timestamp(valid_from),
            valid_to=_parse_timestamp(valid_to) if valid_to is not None else None,
            exclusive=exclusive,
        )
        return {
            "fact_id": fact.block.id,
            "subject": subject,
            "relation": relation,
            "object": object,
            "statement": fact.statement,
        }

    def _query_entity(
        self,
        *,
        name: str,
        as_of: str,
        relation: str | None = None,
    ) -> dict[str, Any]:
        """Implement the ``query_entity`` tool."""

        entity = self._require_entity(name)
        facts = self._graph.facts_at(
            _parse_timestamp(as_of),
            subject=entity,
            relation=relation,
        )
        return {
            "entity": name,
            "as_of": as_of,
            "facts": [_fact_result(fact) for fact in facts],
        }

    def _query_neighborhood(self, *, name: str, as_of: str) -> dict[str, Any]:
        """Implement the ``query_neighborhood`` tool."""

        entity = self._require_entity(name)
        facts = self._graph.neighborhood_at(entity, _parse_timestamp(as_of))
        return {
            "entity": name,
            "as_of": as_of,
            "facts": [_fact_result(fact) for fact in facts],
        }

    def _get_history(self, *, name: str, relation: str) -> dict[str, Any]:
        """Implement the ``get_history`` tool."""

        entity = self._require_entity(name)
        history: list[dict[str, Any]] = []
        for fact in self._graph.history(entity, relation=relation):
            history.append(
                {
                    **_fact_result(fact),
                    "valid_from": _format_timestamp(fact.valid_from),
                    "valid_to": (_format_timestamp(fact.valid_to) if fact.valid_to is not None else None),
                    "sources": [episode.value["source"] for episode in self._graph.provenance(fact)],
                }
            )
        return {"entity": name, "relation": relation, "facts": history}

    def _get_or_create_entity(self, name: str, entity_type: str) -> MemoryBlock:
        """Reuse an exact entity match or create one."""

        matches = self._search_entities(name, entity_type=entity_type)
        if matches:
            return matches[0]
        return self._graph.add_entity(name, entity_type=entity_type)

    def _require_entity(self, name: str) -> MemoryBlock:
        """Return one unambiguous entity by name."""

        matches = self._search_entities(name)
        if not matches:
            raise ValueError(f"Unknown entity {name!r}.")
        if len(matches) > 1:
            raise ValueError(f"Entity name {name!r} is ambiguous; include a type when writing facts.")
        return matches[0]

    def _search_entities(
        self,
        name: str,
        *,
        entity_type: str | None = None,
    ) -> list[MemoryBlock]:
        """Find entities through the graph's bounded memory handle."""

        filters: dict[str, Any] = {"metadata.name": name}
        if entity_type is not None:
            filters["metadata.entity_type"] = entity_type
        return [
            result.block
            for result in self._graph.memory.search(
                types=[ENTITY_TYPE],
                filters=filters,
                limit=2,
            )
        ]


class TemporalGraphAgent:
    """Small agent that learns and answers questions only through tools.

    The decision policy is deterministic so the example is runnable without an
    API key. ``learn`` selects the write tools needed for each observation,
    while ``answer`` selects query tools and composes their results into a
    report. The agent never receives the underlying ``Memory`` or graph object.
    """

    def __init__(self, *, name: str, tools: TemporalGraphTools) -> None:
        """Create an agent with a temporal graph tool set.

        Args:
            name: Human-readable agent name.
            tools: Only interface the agent receives for graph access.
        """

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self.call_log: list[ToolCallRecord] = []

    def learn(self, observations: tuple[Observation, ...]) -> None:
        """Store source episodes and selected facts through agent tools.

        Args:
            observations: Source material and structured fact candidates.
        """

        for observation in observations:
            episode_result = self._call(
                "add_episode",
                {
                    "content": observation.content,
                    "source": observation.source,
                    "observed_at": observation.observed_at,
                },
            )
            for fact in observation.facts:
                self._call(
                    "add_fact",
                    {
                        "subject": fact.subject,
                        "subject_type": fact.subject_type,
                        "relation": fact.relation,
                        "object": fact.object,
                        "object_type": fact.object_type,
                        "episode_id": episode_result["episode_id"],
                        "valid_from": fact.valid_from,
                        "valid_to": fact.valid_to,
                        "exclusive": fact.exclusive,
                    },
                )

    def answer(
        self,
        question: str,
        *,
        subject: str,
        context_entity: str,
        as_of: str,
        history_relation: str,
    ) -> str:
        """Answer a context question using only temporal graph query tools.

        Args:
            question: User-facing question included in the report.
            subject: Entity whose current facts should be retrieved.
            context_entity: Related entity whose neighborhood should be queried.
            as_of: Effective time for current facts and neighborhood queries.
            history_relation: Relationship whose full history should be shown.

        Returns:
            Readable answer assembled from tool results.
        """

        current = self._call("query_entity", {"name": subject, "as_of": as_of})
        neighborhood = self._call(
            "query_neighborhood",
            {"name": context_entity, "as_of": as_of},
        )
        history = self._call(
            "get_history",
            {"name": subject, "relation": history_relation},
        )

        current_statements = {fact["statement"] for fact in current["facts"]}
        related_facts = [fact for fact in neighborhood["facts"] if fact["statement"] not in current_statements]
        lines = [
            f"Question: {question}",
            f"Current facts for {subject} as of {_display_timestamp(as_of)}:",
            *[f"- {fact['statement']}" for fact in current["facts"]],
            f"Related context around {context_entity}:",
            *[f"- {fact['statement']}" for fact in related_facts],
            f"{history_relation} history for {subject}:",
            *[_history_line(fact) for fact in history["facts"]],
        ]
        return "\n".join(lines)

    def tool_usage(self) -> str:
        """Return a compact summary of tools called so far."""

        counts = Counter(record.name for record in self.call_log)
        return ", ".join(f"{name} x{count}" for name, count in counts.items())

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and retain an inspectable agent trace."""

        result = self._toolbox.call(name, arguments)
        self.call_log.append(
            ToolCallRecord(
                name=name,
                arguments=dict(arguments),
                result=dict(result),
            )
        )
        return result


def _fact_result(fact: TemporalFact) -> dict[str, str]:
    """Convert a resolved temporal fact to a JSON-compatible tool result."""

    return {
        "subject": fact.subject.value["name"],
        "relation": fact.relation,
        "object": fact.object.value["name"],
        "statement": fact.statement,
    }


def _history_line(fact: dict[str, Any]) -> str:
    """Render one historical fact returned by ``get_history``."""

    valid_to = _display_timestamp(fact["valid_to"]) if fact["valid_to"] else "open"
    sources = ", ".join(fact["sources"])
    return f"- {fact['statement']}; " f"valid [{_display_timestamp(fact['valid_from'])}, {valid_to}); " f"provenance: {sources}"


def _parse_timestamp(value: str) -> datetime:
    """Parse a timezone-aware ISO 8601 timestamp supplied to a tool."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Tool timestamps must be timezone-aware ISO 8601 values.")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    """Format a timestamp for JSON-compatible tool output."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _display_timestamp(value: str) -> str:
    """Render a tool timestamp as a date for the compact demo report."""

    return _parse_timestamp(value).date().isoformat()
