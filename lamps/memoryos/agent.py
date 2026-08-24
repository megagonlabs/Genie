"""A tool interface and deterministic agent for the MemoryOS lamp.

MemoryOS is used through a handful of calls, and what makes it interesting to
put behind a tool boundary is how much happens that the caller did not ask for.
`add_memory` takes one exchange, and on the way out it may have paged the oldest
exchange into a segment, opened that segment, evicted a colder one, promoted the
hottest one into the profile, and dropped the oldest fact from a knowledge base
to make room. So every tool here returns what the tiers did rather than a
success flag: the call log is where the hierarchy becomes visible.

The other thing worth noticing at this boundary is that retrieving is not free.
`retrieve_memory` raises the heat of every segment it reaches, which is what
eventually promotes one. An agent that keeps asking about seats is an agent that
ends up with a profile that mentions seats.

Every tool set is bound to one user's memory at construction, so no tool
argument can name another user's tiers. The example agent chooses its calls from
explicit inputs rather than from a model, which keeps the demo runnable with no
API key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lamps.memoryos.memoryos import MemoryOS
from lamps.memoryos.model import (
    ASSISTANT,
    USER,
    Exchange,
    KnowledgeOwner,
    exchange_from_json,
    format_timestamp,
    owner_of,
    parse_timestamp,
    positive,
    required_text,
)


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
class ToolCallRecord:
    """One inspectable tool call made by the example agent.

    Attributes:
        name: Invoked tool name.
        arguments: Copy of the JSON-compatible arguments.
        result: Copy of the JSON-compatible result.
    """

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class MemoryOSTools:
    """Memory tools bound to one user's hierarchy, with JSON-compatible calls."""

    def __init__(self, system: MemoryOS, *, user_id: str, assistant_id: str) -> None:
        """Bind one tool set to one user's memory operating system.

        Args:
            system: The hierarchy every call reads and writes.
            user_id: Whose memory this is, shown in the tool descriptions.
            assistant_id: Which assistant it is paired with.
        """

        self.system = system
        self.user_id = required_text(user_id, field="user_id")
        self.assistant_id = required_text(assistant_id, field="assistant_id")
        self.definitions = _definitions(self)
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_memory": self._add_memory,
            "retrieve_memory": self._retrieve_memory,
            "get_user_profile": self._get_user_profile,
            "inspect_sessions": self._inspect_sessions,
            "analyze_now": self._analyze_now,
            "trace_knowledge": self._trace_knowledge,
            "read_dialogue_chain": self._read_dialogue_chain,
            "memory_stats": self._memory_stats,
        }

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke one named tool with JSON-compatible arguments.

        Args:
            name: Tool name.
            arguments: JSON-compatible keyword arguments.

        Returns:
            The JSON-compatible tool result.

        Raises:
            ValueError: If the tool name is unknown.
        """

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown MemoryOS tool {name!r}.")
        return handler(**arguments)

    def _add_memory(self, **exchange: Any) -> dict[str, Any]:
        """Implement `add_memory`, one exchange and everything it sets off."""

        return self.system.add_memory(exchange_from_json(exchange)).as_json()

    def _retrieve_memory(self, *, query: str, at: str) -> dict[str, Any]:
        """Implement `retrieve_memory` across all four tiers."""

        return self.system.retrieve_memory(query, now=parse_timestamp(at, field="at")).as_json()

    def _get_user_profile(self) -> dict[str, Any]:
        """Implement `get_user_profile`, including the readings it replaced."""

        current = self.system.get_user_profile()
        history = self.system.profile_history()
        return {
            "user_id": self.user_id,
            "profile": dict(current.data) if current is not None else {},
            "revision": current.revision if current is not None else 0,
            "history": [revision.as_json() for revision in history],
        }

    def _inspect_sessions(self, *, at: str) -> dict[str, Any]:
        """Implement `inspect_sessions`, the heat table promotion reads."""

        now = parse_timestamp(at, field="at")
        segments = self.system.segments(now=now)
        hottest = max(segments, key=lambda segment: segment.heat, default=None)
        return {
            "at": format_timestamp(now),
            "threshold": self.system.updater.heat_threshold,
            "hottest": hottest.segment_id if hottest is not None else None,
            "sessions": [segment.as_json() for segment in segments],
        }

    def _analyze_now(self, *, at: str) -> dict[str, Any]:
        """Implement `analyze_now`, upstream's forced mid-term analysis."""

        promotion = self.system.force_mid_term_analysis(now=parse_timestamp(at, field="at"))
        return {"promoted": promotion.as_json() if promotion is not None else None}

    def _trace_knowledge(self, *, owner: str, knowledge_id: str) -> dict[str, Any]:
        """Implement `trace_knowledge`, the provenance read upstream cannot make."""

        selected = owner_of(owner)
        item = next((entry for entry in self.system.knowledge(selected) if entry.id == knowledge_id), None)
        if item is None:
            return {"owner": selected, "knowledge_id": knowledge_id, "found": False, "pages": []}
        pages = self.system.sources_of(item.block)
        return {
            "owner": selected,
            "knowledge_id": knowledge_id,
            "found": True,
            "knowledge": item.knowledge,
            "recorded_page_ids": list(item.source_page_ids),
            "pages": [page.as_json() for page in pages],
        }

    def _read_dialogue_chain(self, *, page_id: str, depth: int = 5) -> dict[str, Any]:
        """Implement `read_dialogue_chain`, a walk along the lineage edges."""

        start = self.system.page(page_id)
        following = self.system.dialogue_chain(page_id, depth=positive(depth, field="depth"))
        return {
            "page_id": page_id,
            "found": start is not None,
            "start": start.as_json() if start is not None else None,
            "continues_with": [page.as_json() for page in following],
        }

    def _memory_stats(self) -> dict[str, Any]:
        """Implement `memory_stats`, the counts each tier holds."""

        return {"user_id": self.user_id, "assistant_id": self.assistant_id, **self.system.memory_stats()}


@dataclass(slots=True)
class MemoryOSAgent:
    """A deterministic agent whose only memory access is the tool set.

    Attributes:
        name: Agent name shown in the demo output.
        tools: Tool set bound to one user's hierarchy.
        call_log: Every call the agent made, in order.
    """

    name: str
    tools: MemoryOSTools
    call_log: list[ToolCallRecord] = field(default_factory=list)

    def record(self, exchange: Exchange) -> dict[str, Any]:
        """Hand one exchange to the hierarchy and read back what moved.

        Args:
            exchange: The turn pair, its time, its themed summary, whether it
                continues the previous one, and what an analysis of it would
                conclude.

        Returns:
            The stored exchange plus every page-out, promotion, and eviction the
            call caused.
        """

        return self._call("add_memory", exchange.as_json())

    def recall(self, query: str, *, at: datetime) -> dict[str, Any]:
        """Retrieve context, which also heats every segment the query reaches.

        Args:
            query: Free text to retrieve for.
            at: Time of the retrieval.

        Returns:
            One block of context per tier.
        """

        return self._call("retrieve_memory", {"query": query, "at": format_timestamp(at)})

    def review(self, *, at: datetime) -> dict[str, Any]:
        """Read the heat table that decides what gets promoted next.

        Args:
            at: Time heat is being taken at.

        Returns:
            Every open session with its three heat terms.
        """

        return self._call("inspect_sessions", {"at": format_timestamp(at)})

    def analyze(self, *, at: datetime) -> dict[str, Any]:
        """Promote the hottest session now instead of waiting for it to warm.

        Args:
            at: Time the analysis is taken at.

        Returns:
            What the analysis wrote into long-term memory.
        """

        return self._call("analyze_now", {"at": format_timestamp(at)})

    def trace(self, owner: KnowledgeOwner, knowledge_id: str) -> dict[str, Any]:
        """Ask which pages a stored fact was extracted from.

        Args:
            owner: Whose knowledge base holds the fact.
            knowledge_id: Block id of the entry.

        Returns:
            The pages the analysis read, and which of them are still stored.
        """

        return self._call("trace_knowledge", {"owner": owner, "knowledge_id": knowledge_id})

    def follow(self, page_id: str, *, depth: int = 5) -> dict[str, Any]:
        """Walk the dialogue chain forward from one page.

        Args:
            page_id: Page to start from.
            depth: Maximum edges to follow.

        Returns:
            The pages that continue the conversation.
        """

        return self._call("read_dialogue_chain", {"page_id": page_id, "depth": depth})

    def stats(self) -> dict[str, Any]:
        """Read how much each tier holds.

        Returns:
            Counts per tier, plus the profile's revision number.
        """

        return self._call("memory_stats", {})

    def answer(
        self,
        question: str,
        *,
        query: str,
        at: datetime,
        knowledge_id: str,
        chain_from: str,
        owner: KnowledgeOwner = USER,
    ) -> str:
        """Answer from every tier, and say what each one contributed.

        Args:
            question: User-facing question included in the report.
            query: Retrieval text the tiers are searched with.
            at: Time of the retrieval.
            knowledge_id: Promoted fact to trace back to the pages it came from.
            chain_from: Page to walk the dialogue chain forward from.
            owner: Whose knowledge base holds the traced fact.

        Returns:
            Readable answer assembled from tool results.
        """

        context = self.recall(query, at=at)
        profile = self._call("get_user_profile", {})
        traced = self.trace(owner, knowledge_id)
        chain = self.follow(chain_from)
        stats = self.stats()
        lines = [
            f"Question: {question}",
            f"Profile on file (revision {profile['revision']}):",
            *_profile_lines(profile["profile"]),
            f"Recent exchanges still in short-term memory ({stats['short_term']}/{stats['short_term_capacity']}):",
            *_queue_lines(context["short_term"]),
            f"Mid-term pages retrieved for {query!r}:",
            *_page_lines(context["retrieved_pages"]),
            "What is on file about the user:",
            *_knowledge_lines(context["retrieved_user_knowledge"]),
            "What the assistant learned about doing the job:",
            *_knowledge_lines(context["retrieved_assistant_knowledge"]),
            *_trace_lines(traced),
            "The exchange that started the seat conversation, and what continued it:",
            *_chain_lines(chain),
            "Where the tiers stand:",
            f"- {stats['mid_term_sessions']}/{stats['mid_term_capacity']} sessions holding {stats['mid_term_pages']} page(s)",
            f"- {stats['user_knowledge']} user fact(s) and {stats['assistant_knowledge']} assistant fact(s) promoted",
        ]
        return "\n".join(lines)

    def tool_usage(self) -> dict[str, int]:
        """Return call counts per tool in first-call order.

        Returns:
            Tool names mapped to how often the agent called them.
        """

        return dict(Counter(record.name for record in self.call_log))

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Call one tool and retain an isolated trace record."""

        result = self.tools.call(name, arguments)
        self.call_log.append(ToolCallRecord(name=name, arguments=deepcopy(dict(arguments)), result=deepcopy(result)))
        return result


def _definitions(tools: MemoryOSTools) -> tuple[ToolDefinition, ...]:
    """Describe the tool set, naming the hierarchy every call is bound to."""

    bound = f"{tools.user_id}/{tools.assistant_id}"
    capacity = tools.system.short_term.capacity
    threshold = tools.system.updater.heat_threshold
    return (
        ToolDefinition(
            name="add_memory",
            description=(
                f"Record one exchange for {bound}. A full short-term queue ({capacity}) pages its oldest "
                f"exchange into a session first, and a session over {threshold} heat is analyzed afterwards."
            ),
            parameters={
                "user_input": "what the user said",
                "agent_response": "what the assistant answered",
                "timestamp": "ISO 8601 time the exchange happened",
                "topic": "themed summary a model would have written for it",
                "continues": "true when it continues the previous exchange",
                "insight": "{profile, user_knowledge, assistant_knowledge} an analysis would conclude",
            },
        ),
        ToolDefinition(
            name="retrieve_memory",
            description="Read all four tiers for a query. Every session the query reaches gets hotter.",
            parameters={"query": "string", "at": "ISO 8601 timestamp"},
        ),
        ToolDefinition(
            name="get_user_profile",
            description="Read the profile the analyses arrived at, and every reading it replaced.",
            parameters={},
        ),
        ToolDefinition(
            name="inspect_sessions",
            description="Read each mid-term session with its visit, length, and recency terms.",
            parameters={"at": "ISO 8601 timestamp"},
        ),
        ToolDefinition(
            name="analyze_now",
            description="Analyze the hottest session immediately, whatever its heat.",
            parameters={"at": "ISO 8601 timestamp"},
        ),
        ToolDefinition(
            name="trace_knowledge",
            description="Ask which pages one promoted fact was extracted from.",
            parameters={"owner": f"{USER} or {ASSISTANT}", "knowledge_id": "string"},
        ),
        ToolDefinition(
            name="read_dialogue_chain",
            description="Walk forward from one page along the exchanges that continued it.",
            parameters={"page_id": "string", "depth": "integer"},
        ),
        ToolDefinition(
            name="memory_stats",
            description=f"Read how much each tier of {bound} holds.",
            parameters={},
        ),
    )


def _profile_lines(profile: Mapping[str, Any]) -> list[str]:
    """Render the profile traits, or say nothing has been promoted."""

    if not profile:
        return ["- nothing promoted yet"]
    return [f"- {trait}: {value}" for trait, value in profile.items()]


def _queue_lines(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render the resident queue entries, or say the queue is empty."""

    if not entries:
        return ["- none"]
    return [f"- {entry['user_input']}" for entry in entries]


def _page_lines(pages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render retrieved mid-term pages with the session they came from."""

    if not pages:
        return ["- none"]
    return [f"- [{page['session_summary']}] {page['user_input']} (score {page['score']})" for page in pages]


def _knowledge_lines(hits: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render retrieved knowledge entries, or say none matched."""

    if not hits:
        return ["- none"]
    return [f"- {hit['knowledge']} (score {hit['score']})" for hit in hits]


def _trace_lines(traced: Mapping[str, Any]) -> list[str]:
    """Render where one promoted fact came from, which upstream cannot say."""

    if not traced["found"]:
        return ["That fact is no longer in the knowledge base:", "- it was dropped when the base reached capacity"]
    header = f"Why {traced['knowledge']!r} is on file:"
    if not traced["pages"]:
        return [header, "- the pages it was read from have since been evicted"]
    return [header, *(f"- read from: {page['user_input']!r}" for page in traced["pages"])]


def _chain_lines(chain: Mapping[str, Any]) -> list[str]:
    """Render a dialogue chain as the exchange that opened it and its sequels."""

    if not chain["found"]:
        return ["- that page is no longer in mid-term memory"]
    lines = [f"- {chain['start']['user_input']}"]
    lines.extend(f"- then: {page['user_input']}" for page in chain["continues_with"])
    return lines
