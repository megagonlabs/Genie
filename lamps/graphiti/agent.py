"""A tool interface and deterministic agent for the Graphiti lamp.

Graphiti is a library an agent calls: it ingests episodes, resolves entities,
invalidates contradicted facts, builds communities, and answers hybrid searches.
This module exposes that surface as named tools with JSON-compatible arguments
and results, so an application could hand the same definitions to a
function-calling loop without changing the graph model underneath.

The agent here decides which tools to call from explicit inputs rather than from
a model, which keeps the example runnable with no API key. Entity and fact
extraction is the one upstream step that genuinely needs a language model, so
the agent receives already-extracted triplets — the same shape Graphiti's own
`add_triplet` entry point accepts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from lamps.graphiti.communities import CommunityBuilder
from lamps.graphiti.model import Entity, TemporalFact, format_timestamp, parse_timestamp
from lamps.graphiti.search import RECIPE_NAME, GraphitiSearch, SearchFilters
from lamps.graphiti.temporal_graph import TemporalKnowledgeGraph


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
class TripletCandidate:
    """One subject-relation-object fact extracted from an episode.

    Attributes:
        subject: Subject entity name.
        subject_type: Subject entity type.
        relation: Relation name.
        object: Object entity name.
        object_type: Object entity type.
        valid_from: ISO 8601 time the fact became true.
        valid_to: Optional ISO 8601 time the fact stopped being true.
        fact: Optional readable sentence for the fact.
    """

    subject: str
    subject_type: str
    relation: str
    object: str
    object_type: str
    valid_from: str
    valid_to: str | None = None
    fact: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryUpdate:
    """A refreshed entity summary an episode justifies.

    Attributes:
        entity: Entity name.
        entity_type: Entity type used to resolve the name.
        summary: New summary text.
    """

    entity: str
    entity_type: str
    summary: str


@dataclass(frozen=True, slots=True)
class Observation:
    """One source record plus what the agent extracted from it.

    Attributes:
        name: Short episode name.
        content: Raw episode body.
        reference_time: ISO 8601 time the source event happened.
        ingested_at: ISO 8601 time the graph processes the episode.
        source: Episode kind: `message`, `text`, `json`, or `fact_triple`.
        source_description: Free-text description of the origin.
        triplets: Facts extracted from the episode.
        summaries: Entity summaries the episode justifies refreshing.
    """

    name: str
    content: str
    reference_time: str
    ingested_at: str
    source: str = "text"
    source_description: str = ""
    triplets: tuple[TripletCandidate, ...] = ()
    summaries: tuple[SummaryUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call made by the example agent.

    Attributes:
        name: Invoked tool name.
        arguments: JSON-compatible arguments passed to the tool.
        result: JSON-compatible tool result.
    """

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class TemporalGraphTools:
    """Expose the temporal knowledge graph as agent-callable tools."""

    def __init__(
        self,
        graph: TemporalKnowledgeGraph,
        *,
        communities: CommunityBuilder | None = None,
        search: GraphitiSearch | None = None,
    ) -> None:
        """Create tools backed by one bounded temporal knowledge graph.

        Args:
            graph: Application model that owns episode, entity, and fact logic.
            communities: Optional community builder. Created over `graph` when
                omitted.
            search: Optional search facade. Created over `graph` and the
                community builder when omitted.
        """

        self._graph = graph
        self._communities = communities if communities is not None else CommunityBuilder(graph)
        self._search = search if search is not None else GraphitiSearch(graph)
        self.definitions = (
            ToolDefinition(
                name="add_episode",
                description="Store one source record as the ground truth for later facts.",
                parameters={
                    "name": "string",
                    "content": "string",
                    "reference_time": "ISO 8601 timestamp",
                    "ingested_at": "ISO 8601 timestamp",
                    "source": "message, text, json, or fact_triple",
                    "source_description": "string",
                },
            ),
            ToolDefinition(
                name="add_triplet",
                description=(
                    "Store a subject-relation-object fact from an episode. Entities are "
                    "resolved by type and name, and a single-valued relation invalidates "
                    "the subject's previous fact instead of deleting it."
                ),
                parameters={
                    "subject": "string",
                    "subject_type": "string",
                    "relation": "string",
                    "object": "string",
                    "object_type": "string",
                    "episode_id": "string",
                    "valid_from": "ISO 8601 timestamp",
                    "valid_to": "ISO 8601 timestamp or null",
                    "fact": "optional sentence",
                },
            ),
            ToolDefinition(
                name="update_entity_summary",
                description="Append a new summary revision for an entity, keeping earlier ones.",
                parameters={
                    "entity": "string",
                    "entity_type": "string",
                    "summary": "string",
                },
            ),
            ToolDefinition(
                name="build_communities",
                description="Group the entities one time slice connects and summarize each group.",
                parameters={
                    "as_of": "ISO 8601 timestamp",
                    "known_at": "ISO 8601 timestamp or null",
                },
            ),
            ToolDefinition(
                name="snapshot",
                description=(
                    "Return facts on a bi-temporal slice: true at as_of, and optionally "
                    "restricted to what the graph had on record at known_at."
                ),
                parameters={
                    "as_of": "ISO 8601 timestamp or null",
                    "known_at": "ISO 8601 timestamp or null",
                    "entity": "optional string",
                },
            ),
            ToolDefinition(
                name="get_entity_history",
                description="Return every version of one relationship with both time axes and sources.",
                parameters={"entity": "string", "relation": "string"},
            ),
            ToolDefinition(
                name="search",
                description=(
                    f"Rank facts for a query with the {RECIPE_NAME} recipe, optionally "
                    "centered on an entity and filtered by time."
                ),
                parameters={
                    "query": "string",
                    "center": "optional entity name",
                    "as_of": "ISO 8601 timestamp or null",
                    "known_at": "ISO 8601 timestamp or null",
                },
            ),
            ToolDefinition(
                name="get_community",
                description="Return the community an entity belongs to, with its member summaries.",
                parameters={"entity": "string"},
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_episode": self._add_episode,
            "add_triplet": self._add_triplet,
            "update_entity_summary": self._update_entity_summary,
            "build_communities": self._build_communities,
            "snapshot": self._snapshot,
            "get_entity_history": self._get_entity_history,
            "search": self._search_tool,
            "get_community": self._get_community,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool by name with JSON-compatible arguments.

        Args:
            name: Tool name from `definitions`.
            arguments: Tool arguments.

        Returns:
            JSON-compatible tool result.

        Raises:
            TypeError: If arguments do not match the selected tool.
            ValueError: If the tool name is unknown.
        """

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown temporal graph tool {name!r}.")
        return handler(**arguments)

    def _add_episode(
        self,
        *,
        name: str,
        content: str,
        reference_time: str,
        ingested_at: str,
        source: str = "text",
        source_description: str = "",
    ) -> dict[str, Any]:
        """Implement the `add_episode` tool."""

        episode = self._graph.add_episode(
            name=name,
            content=content,
            reference_time=parse_timestamp(reference_time, field="reference_time"),
            ingested_at=parse_timestamp(ingested_at, field="ingested_at"),
            source=source,  # type: ignore[arg-type]
            source_description=source_description,
        )
        return {
            "episode_id": episode.id,
            "name": episode.name,
            "sequence": episode.sequence,
            "source": episode.source,
        }

    def _add_triplet(
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
        fact: str | None = None,
    ) -> dict[str, Any]:
        """Implement the `add_triplet` tool."""

        result = self._graph.add_triplet(
            subject=subject,
            subject_type=subject_type,
            relation=relation,
            object=object,
            object_type=object_type,
            episode=episode_id,
            valid_at=parse_timestamp(valid_from, field="valid_from"),
            invalid_at=parse_timestamp(valid_to, field="valid_to") if valid_to is not None else None,
            fact=fact,
        )
        return {
            "fact_id": result.fact.id,
            "created": result.created,
            "statement": result.fact.statement,
            "fact": result.fact.fact,
            "invalidated": [
                {
                    "statement": invalid.statement,
                    "invalid_at": _optional_timestamp(invalid.invalid_at),
                    "expired_at": _optional_timestamp(invalid.expired_at),
                }
                for invalid in result.invalidated
            ],
        }

    def _update_entity_summary(
        self,
        *,
        entity: str,
        entity_type: str,
        summary: str,
    ) -> dict[str, Any]:
        """Implement the `update_entity_summary` tool."""

        resolved = self._graph.resolve_entity(entity, entity_type=entity_type)
        updated = self._graph.update_entity_summary(resolved, summary)
        return {
            "entity": updated.name,
            "revision": updated.revision,
            "summary": updated.summary,
        }

    def _build_communities(
        self,
        *,
        as_of: str,
        known_at: str | None = None,
    ) -> dict[str, Any]:
        """Implement the `build_communities` tool."""

        communities = self._communities.build(
            as_of=parse_timestamp(as_of, field="as_of"),
            known_at=parse_timestamp(known_at, field="known_at") if known_at is not None else None,
        )
        return {
            "as_of": as_of,
            "communities": [
                {
                    "name": community.name,
                    "members": list(community.member_names),
                    "summary": community.summary,
                }
                for community in communities
            ],
        }

    def _snapshot(
        self,
        *,
        as_of: str | None = None,
        known_at: str | None = None,
        entity: str | None = None,
    ) -> dict[str, Any]:
        """Implement the `snapshot` tool."""

        center = self._require_entity(entity) if entity is not None else None
        if as_of is not None:
            facts = self._graph.facts_at(
                parse_timestamp(as_of, field="as_of"),
                known_at=parse_timestamp(known_at, field="known_at") if known_at is not None else None,
            )
        elif known_at is not None:
            facts = self._graph.facts_known_at(parse_timestamp(known_at, field="known_at"))
        else:
            raise ValueError("snapshot requires as_of, known_at, or both.")
        if center is not None:
            facts = [fact for fact in facts if center.id in {fact.subject.id, fact.object.id}]
        return {
            "as_of": as_of,
            "known_at": known_at,
            "entity": entity,
            "facts": [_fact_result(fact) for fact in facts],
        }

    def _get_entity_history(self, *, entity: str, relation: str) -> dict[str, Any]:
        """Implement the `get_entity_history` tool."""

        resolved = self._require_entity(entity)
        history = []
        for fact in self._graph.history(resolved, relation=relation):
            replacement = self._graph.superseded_by(fact)
            history.append(
                {
                    **_fact_result(fact),
                    "valid_from": format_timestamp(fact.valid_at),
                    "valid_to": _optional_timestamp(fact.invalid_at),
                    "ingested_at": format_timestamp(fact.ingested_at),
                    "expired_at": _optional_timestamp(fact.expired_at),
                    "sources": [episode.name for episode in self._graph.provenance(fact)],
                    "superseded_by": replacement.statement if replacement is not None else None,
                }
            )
        return {"entity": resolved.name, "relation": relation, "facts": history}

    def _search_tool(
        self,
        *,
        query: str,
        center: str | None = None,
        as_of: str | None = None,
        known_at: str | None = None,
    ) -> dict[str, Any]:
        """Implement the `search` tool."""

        hits = self._search.search(
            query,
            center=self._require_entity(center) if center is not None else None,
            filters=SearchFilters(
                as_of=parse_timestamp(as_of, field="as_of") if as_of is not None else None,
                known_at=parse_timestamp(known_at, field="known_at") if known_at is not None else None,
            ),
        )
        return {
            "query": query,
            "recipe": RECIPE_NAME,
            "hits": [
                {"text": hit.text, "score": hit.score, "reason": hit.reason}
                for hit in hits
            ],
        }

    def _get_community(self, *, entity: str) -> dict[str, Any]:
        """Implement the `get_community` tool."""

        resolved = self._require_entity(entity)
        community = self._communities.community_of(resolved)
        if community is None:
            return {"entity": resolved.name, "community": None}
        return {
            "entity": resolved.name,
            "community": {
                "name": community.name,
                "members": list(community.member_names),
                "summary": community.summary,
            },
        }

    def _require_entity(self, name: str) -> Entity:
        """Resolve one unambiguous entity by name."""

        entity = self._graph.get_entity(name)
        if entity is None:
            raise ValueError(f"Unknown entity {name!r}.")
        return entity


class TemporalGraphAgent:
    """Small agent that learns and answers only through graph tools.

    The decision policy is deterministic so the example runs without an API
    key. `learn` selects the write tools each observation needs; `answer`
    selects the read tools and composes their results into a report. The agent
    never receives the underlying `Memory`, handle, or graph object.
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

    def learn(self, observations: Sequence[Observation], *, communities_as_of: str) -> None:
        """Ingest observations, then rebuild the community subgraph.

        Args:
            observations: Source records and what was extracted from them.
            communities_as_of: Effective time whose valid facts define the
                communities built after ingestion.
        """

        for observation in observations:
            episode = self._call(
                "add_episode",
                {
                    "name": observation.name,
                    "content": observation.content,
                    "reference_time": observation.reference_time,
                    "ingested_at": observation.ingested_at,
                    "source": observation.source,
                    "source_description": observation.source_description,
                },
            )
            for triplet in observation.triplets:
                self._call(
                    "add_triplet",
                    {
                        "subject": triplet.subject,
                        "subject_type": triplet.subject_type,
                        "relation": triplet.relation,
                        "object": triplet.object,
                        "object_type": triplet.object_type,
                        "episode_id": episode["episode_id"],
                        "valid_from": triplet.valid_from,
                        "valid_to": triplet.valid_to,
                        "fact": triplet.fact,
                    },
                )
            for update in observation.summaries:
                self._call(
                    "update_entity_summary",
                    {
                        "entity": update.entity,
                        "entity_type": update.entity_type,
                        "summary": update.summary,
                    },
                )
        self._call("build_communities", {"as_of": communities_as_of, "known_at": None})

    def answer(
        self,
        question: str,
        *,
        subject: str,
        as_of: str,
        known_at: str,
        history_relation: str,
        query: str,
    ) -> str:
        """Answer a context question using only graph read tools.

        Args:
            question: User-facing question included in the report.
            subject: Entity the question is about.
            as_of: Effective time for the current-facts snapshot.
            known_at: Earlier ingestion time used to show what the graph had on
                record before the latest episodes arrived.
            history_relation: Relationship whose full history is reported.
            query: Natural-language query passed to the search recipes.

        Returns:
            Readable answer assembled from tool results.
        """

        current = self._call("snapshot", {"as_of": as_of, "entity": subject})
        earlier = self._call("snapshot", {"known_at": known_at, "entity": subject})
        history = self._call(
            "get_entity_history",
            {"entity": subject, "relation": history_relation},
        )
        ranked = self._call(
            "search",
            {"query": query, "center": subject, "as_of": as_of},
        )
        context = self._call("get_community", {"entity": subject})
        community = context["community"]

        lines = [
            f"Question: {question}",
            f"True for {subject} as of {_date(as_of)}:",
            *[f"- {fact['statement']}" for fact in current["facts"]],
            f"On record for {subject} on {_date(known_at)}:",
            *[f"- {fact['statement']}" for fact in earlier["facts"]],
            f"{history_relation} history for {subject}:",
            *[_history_line(fact) for fact in history["facts"]],
            f"Top facts for {query!r}:",
            *[f"- {hit['text']} (score {hit['score']:.3f})" for hit in ranked["hits"][:3]],
            "Community context:",
            f"- {community['name']}: {community['summary']}" if community else "- none",
        ]
        return "\n".join(lines)

    def tool_usage(self) -> str:
        """Return a compact summary of tools called so far.

        Returns:
            Comma-separated `name xcount` pairs in first-call order.
        """

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


def _fact_result(fact: TemporalFact) -> dict[str, Any]:
    """Convert a resolved fact into a JSON-compatible tool result."""

    return {
        "fact_id": fact.id,
        "subject": fact.subject.value["name"],
        "relation": fact.relation,
        "object": fact.object.value["name"],
        "statement": fact.statement,
        "fact": fact.fact,
    }


def _history_line(fact: dict[str, Any]) -> str:
    """Render one historical fact returned by `get_entity_history`."""

    valid_to = _date(fact["valid_to"]) if fact["valid_to"] else "open"
    expired_at = _date(fact["expired_at"]) if fact["expired_at"] else "open"
    sources = ", ".join(fact["sources"])
    return (
        f"- {fact['statement']}; true [{_date(fact['valid_from'])}, {valid_to}); "
        f"on record [{_date(fact['ingested_at'])}, {expired_at}); sources: {sources}"
    )


def _optional_timestamp(value: Any) -> str | None:
    """Format an optional timestamp for JSON-compatible tool output."""

    return format_timestamp(value) if value is not None else None


def _date(value: str) -> str:
    """Render a tool timestamp as a date for the compact demo report."""

    return parse_timestamp(value).date().isoformat()
