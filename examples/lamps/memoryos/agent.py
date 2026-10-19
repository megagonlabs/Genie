"""A tool interface and a deterministic agent for the MemoryOS lamp.

An agent uses MemoryOS through a small number of calls. A tool boundary is
useful here, because much occurs that the caller did not ask for:

- **`add_memory` has many effects:** it takes one exchange. Before it returns,
  it can page the oldest exchange into a segment, open that segment, evict a
  colder segment, promote the hottest segment into the profile, and remove the
  oldest fact from a knowledge base to make room.
- **Results, not success flags:** so each tool here returns what the tiers did.
  The call log shows the hierarchy.
- **Retrieval has an effect:** `retrieve_memory` raises the heat of each segment
  that it reaches, and heat promotes a segment at a later time. An agent that
  asks about seats again and again gets a profile that mentions seats.
- **Bound to one user:** each tool set is bound to the memory of one user when
  it is created. So no tool argument can name the tiers of another user.
- **No model:** the example agent selects its calls from explicit inputs and not
  from a model. So the demo runs with no API key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from examples.lamps.memoryos.memoryos import MemoryOS
from examples.lamps.memoryos.model import (
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
    """Description of a tool that an agent receives.

    Attributes:
        name: Stable tool name for calls.
        description: Plain-language description for the agent.
        parameters: Mapping of argument names to short type descriptions.
    """

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call of the example agent, kept for inspection.

    Attributes:
        name: Name of the called tool.
        arguments: Copy of the JSON-compatible arguments.
        result: Copy of the JSON-compatible result.
    """

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class MemoryOSTools:
    """Memory tools bound to the hierarchy of one user, with JSON-compatible calls."""

    def __init__(self, system: MemoryOS, *, user_id: str, assistant_id: str) -> None:
        """Bind one tool set to the memory operating system of one user.

        Args:
            system: The hierarchy that each call reads and writes.
            user_id: The owner of this memory. The tool descriptions show it.
            assistant_id: The assistant that this memory is paired with.
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
        """Call one named tool with JSON-compatible arguments.

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
        """Implement `add_memory`: store one exchange, and return all effects that it started."""

        return self.system.add_memory(exchange_from_json(exchange)).as_json()

    def _retrieve_memory(self, *, query: str, at: str) -> dict[str, Any]:
        """Implement `retrieve_memory` across all 4 tiers."""

        return self.system.retrieve_memory(query, now=parse_timestamp(at, field="at")).as_json()

    def _get_user_profile(self) -> dict[str, Any]:
        """Implement `get_user_profile`, with the old profiles that the current profile replaced."""

        current = self.system.get_user_profile()
        history = self.system.profile_history()
        return {
            "user_id": self.user_id,
            "profile": dict(current.data) if current is not None else {},
            "revision": current.revision if current is not None else 0,
            "history": [revision.as_json() for revision in history],
        }

    def _inspect_sessions(self, *, at: str) -> dict[str, Any]:
        """Implement `inspect_sessions`, which returns the heat table that promotion reads."""

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
        """Implement `analyze_now`, the forced mid-term analysis of upstream."""

        promotion = self.system.force_mid_term_analysis(now=parse_timestamp(at, field="at"))
        return {"promoted": promotion.as_json() if promotion is not None else None}

    def _trace_knowledge(self, *, owner: str, knowledge_id: str) -> dict[str, Any]:
        """Implement `trace_knowledge`, the provenance read that upstream cannot do."""

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
        """Implement `memory_stats`, which returns the counts of each tier."""

        return {"user_id": self.user_id, "assistant_id": self.assistant_id, **self.system.memory_stats()}


@dataclass(slots=True)
class MemoryOSAgent:
    """A deterministic agent that uses memory only through the tool set.

    Attributes:
        name: Agent name in the demo output.
        tools: Tool set bound to the hierarchy of one user.
        call_log: Each call that the agent made, in order.
    """

    name: str
    tools: MemoryOSTools
    call_log: list[ToolCallRecord] = field(default_factory=list)

    def record(self, exchange: Exchange) -> dict[str, Any]:
        """Give one exchange to the hierarchy, and return what moved.

        Args:
            exchange: The user turn and the reply, its time, its themed summary,
                a flag that tells if it continues the previous exchange, and
                what an analysis of it would conclude.

        Returns:
            The stored exchange, and each page-out, promotion, and eviction that
            the call caused.
        """

        return self._call("add_memory", exchange.as_json())

    def recall(self, query: str, *, at: datetime) -> dict[str, Any]:
        """Retrieve context. This also makes each segment that the query reaches hotter.

        Args:
            query: Free text to retrieve context for.
            at: Time of the retrieval.

        Returns:
            One part of the context for each tier.
        """

        return self._call("retrieve_memory", {"query": query, "at": format_timestamp(at)})

    def review(self, *, at: datetime) -> dict[str, Any]:
        """Read the heat table that decides the next promotion.

        Args:
            at: Time for the heat calculation.

        Returns:
            Each open session with its 3 heat terms.
        """

        return self._call("inspect_sessions", {"at": format_timestamp(at)})

    def analyze(self, *, at: datetime) -> dict[str, Any]:
        """Promote the hottest session now, and do not wait until it becomes hot.

        Args:
            at: Time of the analysis.

        Returns:
            What the analysis wrote into long-term memory.
        """

        return self._call("analyze_now", {"at": format_timestamp(at)})

    def trace(self, owner: KnowledgeOwner, knowledge_id: str) -> dict[str, Any]:
        """Ask which pages a stored fact came from.

        Args:
            owner: The owner of the knowledge base that holds the fact.
            knowledge_id: Block id of the entry.

        Returns:
            The ids of the pages that the analysis read, and the pages that are
            still stored.
        """

        return self._call("trace_knowledge", {"owner": owner, "knowledge_id": knowledge_id})

    def follow(self, page_id: str, *, depth: int = 5) -> dict[str, Any]:
        """Walk the dialogue chain forward from one page.

        Args:
            page_id: Page to start from.
            depth: Maximum number of edges to follow.

        Returns:
            The pages that continue the conversation.
        """

        return self._call("read_dialogue_chain", {"page_id": page_id, "depth": depth})

    def stats(self) -> dict[str, Any]:
        """Read the quantity in each tier.

        Returns:
            Counts for each tier, and the revision number of the profile.
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
        """Answer from each tier, and tell what each tier gave.

        Args:
            question: The question for the user, which the report shows.
            query: Retrieval text for the search of the tiers.
            at: Time of the retrieval.
            knowledge_id: Promoted fact to trace back to the pages that it came
                from.
            chain_from: Page from which the walk along the dialogue chain
                starts.
            owner: The owner of the knowledge base that holds the traced fact.

        Returns:
            Readable answer made from the tool results.
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
        """Return the number of calls for each tool, in first-call order.

        Returns:
            Tool names mapped to the number of times that the agent called them.
        """

        return dict(Counter(record.name for record in self.call_log))

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Call one tool, and keep a trace record with separate copies of the arguments and the result."""

        result = self.tools.call(name, arguments)
        self.call_log.append(ToolCallRecord(name=name, arguments=deepcopy(dict(arguments)), result=deepcopy(result)))
        return result


def _definitions(tools: MemoryOSTools) -> tuple[ToolDefinition, ...]:
    """Describe the tool set, and name the hierarchy that each call is bound to."""

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
    """Format the profile traits, or tell that no promotion set traits yet."""

    if not profile:
        return ["- nothing promoted yet"]
    return [f"- {trait}: {value}" for trait, value in profile.items()]


def _queue_lines(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Format the entries in the queue, or tell that the queue is empty."""

    if not entries:
        return ["- none"]
    return [f"- {entry['user_input']}" for entry in entries]


def _page_lines(pages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Format the retrieved mid-term pages with the session of each page, or tell that none matched."""

    if not pages:
        return ["- none"]
    return [f"- [{page['session_summary']}] {page['user_input']} (score {page['score']})" for page in pages]


def _knowledge_lines(hits: Sequence[Mapping[str, Any]]) -> list[str]:
    """Format the retrieved knowledge entries, or tell that no entry matched."""

    if not hits:
        return ["- none"]
    return [f"- {hit['knowledge']} (score {hit['score']})" for hit in hits]


def _trace_lines(traced: Mapping[str, Any]) -> list[str]:
    """Format the source of one promoted fact, which upstream cannot tell."""

    if not traced["found"]:
        return ["That fact is no longer in the knowledge base:", "- it was dropped when the base reached capacity"]
    header = f"Why {traced['knowledge']!r} is on file:"
    if not traced["pages"]:
        return [header, "- the pages it was read from have since been evicted"]
    return [header, *(f"- read from: {page['user_input']!r}" for page in traced["pages"])]


def _chain_lines(chain: Mapping[str, Any]) -> list[str]:
    """Format a dialogue chain as the exchange that started it and the exchanges that followed."""

    if not chain["found"]:
        return ["- that page is no longer in mid-term memory"]
    lines = [f"- {chain['start']['user_input']}"]
    lines.extend(f"- then: {page['user_input']}" for page in chain["continues_with"])
    return lines
