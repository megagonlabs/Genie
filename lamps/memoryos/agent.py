"""Agent-callable tools for the MemoryOS-inspired lamp."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lamps.memoryos.memory_system import (
    KnowledgeDraft,
    KnowledgeEntry,
    MemoryOSMemory,
    MidTermPage,
    ShortTermQA,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of one tool presented to the example agent."""

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class ConversationCandidate:
    """One QA pair the agent selected for memory."""

    key: str
    user_input: str
    agent_response: str
    occurred_at: str
    summary: str
    entities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    """One long-term fact synthesized from selected mid-term pages."""

    knowledge: str
    source_keys: tuple[str, ...]
    entities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LongTermCandidate:
    """Profile and knowledge updates selected after page reflection."""

    profile_updates: dict[str, Any]
    profile_source_keys: tuple[str, ...]
    user_knowledge: tuple[KnowledgeCandidate, ...] = ()
    assistant_knowledge: tuple[KnowledgeCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One inspectable memory-tool call made by the agent."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class MemoryOSTools:
    """Expose a bounded MemoryOS-inspired service as agent tools."""

    def __init__(self, memory: MemoryOSMemory) -> None:
        """Create named JSON-compatible tools over one memory hierarchy."""

        self._memory = memory
        self.definitions = (
            ToolDefinition(
                name="add_memory",
                description="Append a short-term QA pair and process FIFO overflow into pages.",
                parameters={
                    "key": "string",
                    "user_input": "string",
                    "agent_response": "string",
                    "occurred_at": "ISO 8601 timestamp",
                    "summary": "promotion summary",
                    "entities": "list[string]",
                },
            ),
            ToolDefinition(
                name="retrieve_memory",
                description="Retrieve all tiers and heat the sessions containing matching pages.",
                parameters={"query": "string", "limit": "integer"},
            ),
            ToolDefinition(
                name="update_long_term",
                description="Promote pages from hot sessions into profile and knowledge collections.",
                parameters={
                    "profile_updates": "object",
                    "profile_source_page_ids": "list[page id]",
                    "user_knowledge": "list[knowledge draft]",
                    "assistant_knowledge": "list[knowledge draft]",
                },
            ),
            ToolDefinition(
                name="get_user_profile",
                description="Retrieve the current long-term user-profile dictionary.",
                parameters={},
            ),
            ToolDefinition(
                name="retrieve_temporal",
                description="Retrieve dated short-, mid-, and long-term entries in an interval.",
                parameters={
                    "start": "ISO 8601 timestamp",
                    "end": "ISO 8601 timestamp",
                    "limit": "integer",
                },
            ),
            ToolDefinition(
                name="retrieve_graph",
                description="Traverse page chronology and mid-to-long promotion links.",
                parameters={
                    "anchor_page_id": "page id",
                    "max_depth": "integer",
                    "limit": "integer",
                },
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_memory": self._add_memory,
            "retrieve_memory": self._retrieve_memory,
            "update_long_term": self._update_long_term,
            "get_user_profile": self._get_user_profile,
            "retrieve_temporal": self._retrieve_temporal,
            "retrieve_graph": self._retrieve_graph,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one named memory tool."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown MemoryOS tool {name!r}.")
        return handler(**arguments)

    def _add_memory(
        self,
        *,
        key: str,
        user_input: str,
        agent_response: str,
        occurred_at: str,
        summary: str,
        entities: list[str] | None = None,
    ) -> dict[str, Any]:
        result = self._memory.add_memory(
            key=key,
            user_input=user_input,
            agent_response=agent_response,
            occurred_at=_parse_timestamp(occurred_at, field="occurred_at"),
            summary=summary,
            entities=entities or (),
        )
        return {
            "short_term_qa": _qa_result(result.qa),
            "promoted_pages": [_page_result(page) for page in result.promoted_pages],
        }

    def _retrieve_memory(self, *, query: str, limit: int = 5) -> dict[str, Any]:
        bundle = self._memory.retrieve_memory(query, limit=limit)
        return {
            "short_term": [_qa_result(qa) for qa in bundle.short_term],
            "mid_term": [_page_result(page) for page in bundle.mid_term],
            "user_profile": (
                deepcopy(bundle.user_profile.data) if bundle.user_profile is not None else None
            ),
            "user_knowledge": [_knowledge_result(item) for item in bundle.user_knowledge],
            "assistant_knowledge": [
                _knowledge_result(item) for item in bundle.assistant_knowledge
            ],
        }

    def _update_long_term(
        self,
        *,
        profile_updates: dict[str, Any],
        profile_source_page_ids: list[str],
        user_knowledge: list[dict[str, Any]] | None = None,
        assistant_knowledge: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        profile, user_entries, assistant_entries = self._memory.update_long_term(
            profile_updates=profile_updates,
            profile_source_page_ids=profile_source_page_ids,
            user_knowledge=tuple(
                _knowledge_draft(value) for value in (user_knowledge or ())
            ),
            assistant_knowledge=tuple(
                _knowledge_draft(value) for value in (assistant_knowledge or ())
            ),
        )
        return {
            "user_profile": deepcopy(profile.data),
            "user_knowledge": [_knowledge_result(item) for item in user_entries],
            "assistant_knowledge": [
                _knowledge_result(item) for item in assistant_entries
            ],
        }

    def _get_user_profile(self) -> dict[str, Any]:
        profile = self._memory.get_user_profile()
        return {"user_profile": deepcopy(profile.data) if profile is not None else None}

    def _retrieve_temporal(
        self,
        *,
        start: str,
        end: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        results = self._memory.retrieve_temporal(
            _parse_timestamp(start, field="start"),
            _parse_timestamp(end, field="end"),
            limit=limit,
        )
        return {
            "results": [
                {
                    "layer": result.layer,
                    "item_id": result.item_id,
                    "occurred_at": _format_timestamp(result.occurred_at),
                    "text": result.text,
                }
                for result in results
            ]
        }

    def _retrieve_graph(
        self,
        *,
        anchor_page_id: str,
        max_depth: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        results = self._memory.retrieve_graph(
            anchor_page_id,
            max_depth=max_depth,
            limit=limit,
        )
        return {
            "results": [
                {
                    "item_id": result.block.id,
                    "item_type": result.block.type,
                    "relation": result.relation,
                    "direction": result.direction,
                    "distance": result.distance,
                    "description": result.description,
                }
                for result in results
            ]
        }


class MemoryOSAgent:
    """Deterministic agent that uses tools for all memory operations."""

    def __init__(self, *, name: str, tools: MemoryOSTools) -> None:
        """Create an agent with no direct access to the memory hierarchy."""

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self.call_log: list[ToolCallRecord] = []
        self._page_ids: dict[str, str] = {}

    def learn(
        self,
        *,
        conversations: tuple[ConversationCandidate, ...],
        reflection_query: str,
        long_term: LongTermCandidate,
    ) -> None:
        """Add QA pairs, heat relevant pages, and consolidate long-term memory."""

        for candidate in conversations:
            result = self._call(
                "add_memory",
                {
                    "key": candidate.key,
                    "user_input": candidate.user_input,
                    "agent_response": candidate.agent_response,
                    "occurred_at": candidate.occurred_at,
                    "summary": candidate.summary,
                    "entities": list(candidate.entities),
                },
            )
            for page in result["promoted_pages"]:
                self._page_ids[page["source_key"]] = page["page_id"]

        self._call("retrieve_memory", {"query": reflection_query, "limit": 10})
        self._call(
            "update_long_term",
            {
                "profile_updates": deepcopy(long_term.profile_updates),
                "profile_source_page_ids": [
                    self._required_page_id(key) for key in long_term.profile_source_keys
                ],
                "user_knowledge": [
                    _knowledge_candidate_result(candidate, self._page_ids)
                    for candidate in long_term.user_knowledge
                ],
                "assistant_knowledge": [
                    _knowledge_candidate_result(candidate, self._page_ids)
                    for candidate in long_term.assistant_knowledge
                ],
            },
        )

    def answer(
        self,
        question: str,
        *,
        query: str,
        start: str,
        end: str,
        graph_anchor_key: str,
    ) -> str:
        """Combine profile, multi-tier, temporal, and graph retrieval."""

        profile = self._call("get_user_profile", {})["user_profile"]
        memories = self._call("retrieve_memory", {"query": query, "limit": 5})
        temporal = self._call(
            "retrieve_temporal",
            {"start": start, "end": end, "limit": 20},
        )["results"]
        graph = self._call(
            "retrieve_graph",
            {
                "anchor_page_id": self._required_page_id(graph_anchor_key),
                "max_depth": 2,
                "limit": 10,
            },
        )["results"]

        profile_text = json_profile(profile)
        facts = [
            *(item["knowledge"] for item in memories["user_knowledge"]),
            *(item["knowledge"] for item in memories["assistant_knowledge"]),
        ]
        fact_lines = "\n".join(f"- {fact}" for fact in facts) or "- No matching facts."
        timeline_lines = "\n".join(
            f"- {result['occurred_at'][:10]} [{result['layer']}]: {result['text']}"
            for result in temporal
        ) or "- No dated memories."
        graph_lines = "\n".join(
            f"- {result['relation']} ({result['direction']}, depth {result['distance']}): "
            f"{result['description']}"
            for result in graph
        ) or "- No graph context."
        return (
            f"Question: {question}\n"
            f"Long-term user profile: {profile_text}\n"
            f"Relevant long-term facts:\n{fact_lines}\n"
            f"Experience timeline:\n{timeline_lines}\n"
            f"Graph context:\n{graph_lines}"
        )

    def page_id(self, source_key: str) -> str:
        """Return the page created from one demo conversation key."""

        return self._required_page_id(source_key)

    def tool_usage(self) -> dict[str, int]:
        """Return usage counts in first-call order."""

        return dict(Counter(call.name for call in self.call_log))

    def _required_page_id(self, source_key: str) -> str:
        page_id = self._page_ids.get(source_key)
        if page_id is None:
            raise ValueError(f"Conversation {source_key!r} has not reached mid-term memory.")
        return page_id

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._toolbox.call(name, arguments)
        self.call_log.append(ToolCallRecord(name, deepcopy(arguments), deepcopy(result)))
        return result


def _qa_result(qa: ShortTermQA) -> dict[str, Any]:
    return {
        "id": qa.block.id,
        "key": qa.key,
        "user_input": qa.user_input,
        "agent_response": qa.agent_response,
        "timestamp": _format_timestamp(qa.occurred_at),
    }


def _page_result(page: MidTermPage) -> dict[str, Any]:
    return {
        "page_id": page.id,
        "session_id": page.session_id,
        "source_key": page.source_key,
        "summary": page.summary,
        "dialogue": {
            "user_input": page.user_input,
            "agent_response": page.agent_response,
            "timestamp": _format_timestamp(page.occurred_at),
        },
        "segment_heat": page.segment_heat,
        "segment_visit_count": page.segment_visit_count,
    }


def _knowledge_result(item: KnowledgeEntry) -> dict[str, Any]:
    return {
        "id": item.block.id,
        "owner": item.owner,
        "knowledge": item.knowledge,
        "timestamp": _format_timestamp(item.timestamp),
        "source_page_ids": list(item.source_page_ids),
    }


def _knowledge_draft(value: dict[str, Any]) -> KnowledgeDraft:
    return KnowledgeDraft(
        knowledge=value["knowledge"],
        source_page_ids=tuple(value["source_page_ids"]),
        entities=tuple(value.get("entities", [])),
    )


def _knowledge_candidate_result(
    candidate: KnowledgeCandidate,
    page_ids: dict[str, str],
) -> dict[str, Any]:
    try:
        source_ids = [page_ids[key] for key in candidate.source_keys]
    except KeyError as exc:
        raise ValueError(f"Conversation {exc.args[0]!r} has not reached mid-term memory.") from exc
    return {
        "knowledge": candidate.knowledge,
        "source_page_ids": source_ids,
        "entities": list(candidate.entities),
    }


def json_profile(profile: dict[str, Any] | None) -> str:
    """Render the compact demo profile without exposing storage details."""

    if profile is None:
        return "No profile available."
    seat = profile.get("seat_preference", "unknown seat")
    hotel = profile.get("austin_hotel", "unknown hotel")
    strategy = profile.get("planning_strategy", "no planning strategy")
    return f"seat={seat}; Austin hotel={hotel}; strategy={strategy}"


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
