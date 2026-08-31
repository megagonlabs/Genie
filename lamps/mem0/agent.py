"""A tool interface and deterministic agent for the Mem0 lamp.

Mem0 is used through a small set of calls, and the interesting thing about them
is how little the caller controls.

- An agent hands over an exchange. What gets stored, replaced, or removed is
  decided by the pipeline, and the agent finds out afterwards by reading the
  events it returns.
- That asymmetry is why the tools here return the decisions rather than a
  success flag. The call log is where the agent's memory writes become
  inspectable.
- The user and agent identifiers are bound once, at construction, so no tool
  argument can name another user's memories.
- A run is a tool argument, because a run is one conversation and upstream
  passes it per call. It can only narrow the bound scope, never leave it.
- The example agent chooses its calls from explicit inputs rather than from a
  model, which keeps the demo runnable with no API key. Extraction and the
  add-or-replace-or-forget judgment are the two steps that genuinely need a
  model upstream, so the agent receives candidate facts and relationships
  already formed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from lamps.mem0.client import Mem0Memory
from lamps.mem0.model import (
    FACTUAL,
    PROCEDURAL,
    Exchange,
    Session,
    facts_from_json,
    json_mapping,
    messages_from_json,
    optional_text,
    parse_timestamp,
    required_text,
    triplets_from_json,
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


class Mem0Tools:
    """Scope-bound memory tools with JSON-compatible arguments."""

    def __init__(self, client: Mem0Memory, *, session: Session) -> None:
        """Bind one tool set to one user and agent.

        Args:
            client: Memory client the tools call.
            session: Scope every call is made inside, normally naming the
                user and agent only. A `run_id` on it becomes the default run,
                which a call may replace; the user and agent cannot be named by
                any tool argument at all.
        """

        self.client = client
        self.session = session
        self.definitions = _definitions(session, client)
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "add_memories": self._add_memories,
            "search_memories": self._search_memories,
            "list_memories": self._list_memories,
            "update_memory": self._update_memory,
            "delete_memory": self._delete_memory,
            "memory_history": self._memory_history,
            "forget_scope": self._forget_scope,
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
            raise ValueError(f"Unknown Mem0 tool {name!r}.")
        return handler(**arguments)

    def scope(self, *, run: str | None = None, actor: str | None = None) -> Session:
        """Return the session one call runs in.

        Args:
            run: Run to narrow to, or `None` to keep the bound run.
            actor: Speaker to credit, or `None` to keep the bound one.

        Returns:
            A session inside the bound user and agent.
        """

        selected = optional_text(run, field="run")
        return Session(
            user_id=self.session.user_id,
            agent_id=self.session.agent_id,
            run_id=selected or self.session.run_id,
            actor_id=optional_text(actor, field="actor") or self.session.actor_id,
        )

    def _add_memories(
        self,
        *,
        at: str,
        messages: Sequence[Mapping[str, Any]] = (),
        facts: Sequence[Mapping[str, Any]] = (),
        relations: Sequence[Mapping[str, Any]] = (),
        infer: bool = True,
        memory_type: str = FACTUAL,
        metadata: Mapping[str, Any] | None = None,
        run: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Implement `add_memories`, the whole two-phase write."""

        result = self.client.add(
            self.scope(run=run, actor=actor),
            messages_from_json(messages),
            facts=facts_from_json(facts),
            relations=triplets_from_json(relations),
            infer=bool(infer),
            memory_type=required_text(memory_type, field="memory_type"),
            metadata=json_mapping(metadata, field="metadata") or None,
            at=parse_timestamp(at, field="at"),
        )
        return result.as_json()

    def _search_memories(
        self,
        *,
        query: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        threshold: float = 0.0,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Implement `search_memories` over the scope and the graph together."""

        result = self.client.search(
            self.scope(run=run),
            query,
            filters=json_mapping(filters, field="filters") or None,
            limit=limit,
            threshold=threshold,
        )
        return {"filters": json_mapping(filters, field="filters"), "threshold": threshold, **result.as_json()}

    def _list_memories(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Implement `list_memories` for one scope."""

        scope = self.scope(run=run)
        memories = self.client.get_all(scope, filters=json_mapping(filters, field="filters") or None, limit=limit)
        return {
            "scope": scope.label,
            "results": [memory.as_json() for memory in memories],
            "relations": [relation.as_json() for relation in self.client.relations(scope)],
        }

    def _update_memory(
        self,
        *,
        memory_id: str,
        text: str,
        at: str,
        metadata: Mapping[str, Any] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Implement `update_memory`, the caller's own correction."""

        change = self.client.update(
            memory_id,
            text,
            metadata=json_mapping(metadata, field="metadata") or None,
            actor_id=optional_text(actor, field="actor"),
            at=parse_timestamp(at, field="at"),
        )
        return change.as_json()

    def _delete_memory(self, *, memory_id: str, at: str, actor: str | None = None) -> dict[str, Any]:
        """Implement `delete_memory`, the caller's own removal."""

        change = self.client.delete(
            memory_id,
            actor_id=optional_text(actor, field="actor"),
            at=parse_timestamp(at, field="at"),
        )
        return change.as_json()

    def _memory_history(self, *, memory_id: str) -> dict[str, Any]:
        """Implement `memory_history` for one memory, deleted or not."""

        entries = self.client.history(memory_id)
        return {
            "memory_id": memory_id,
            "exists": self.client.get(memory_id) is not None,
            "events": [entry.as_json() for entry in entries],
        }

    def _forget_scope(self, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Implement `forget_scope`, one subtree delete for a whole scope."""

        scope = self.scope(run=run)
        changes = self.client.delete_all(scope, at=parse_timestamp(at, field="at"))
        return {"scope": scope.label, "removed": [change.as_json() for change in changes]}


@dataclass(slots=True)
class Mem0Agent:
    """A deterministic agent whose only memory access is the tool set.

    Attributes:
        name: Agent name shown in the demo output.
        tools: Scope-bound tool set.
        call_log: Every call the agent made, in order.
    """

    name: str
    tools: Mem0Tools
    call_log: list[ToolCallRecord] = field(default_factory=list)

    def learn(self, exchange: Exchange, *, at: str, run: str | None = None, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Hand one exchange to the pipeline and read back what it decided.

        Args:
            exchange: Turns, candidate facts, and candidate relationships.
            at: ISO 8601 time recorded on every write the call makes.
            run: Run to write in, defaulting to the bound one.
            metadata: Application metadata to keep with each memory.

        Returns:
            The memory events and relationship changes the call produced.
        """

        return self._call(
            "add_memories",
            {
                "messages": [message.as_json() for message in exchange.messages],
                "facts": [
                    {"text": fact.text, "retracts": fact.retracts, "actor_id": fact.actor_id, "role": fact.role}
                    for fact in exchange.facts
                ],
                "relations": [
                    {
                        "source": triplet.source,
                        "relationship": triplet.relation,
                        "destination": triplet.target,
                        "retracts": triplet.retracts,
                    }
                    for triplet in exchange.relations
                ],
                "at": at,
                "run": run,
                "metadata": dict(metadata) if metadata else None,
            },
        )

    def transcribe(self, exchange: Exchange, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Store an exchange as it was said, with no extraction at all.

        Args:
            exchange: Turns to keep verbatim.
            at: ISO 8601 time recorded on the writes.
            run: Run to write in, defaulting to the bound one.

        Returns:
            One `ADD` per kept turn.
        """

        return self._call(
            "add_memories",
            {
                "messages": [message.as_json() for message in exchange.messages],
                "infer": False,
                "at": at,
                "run": run,
            },
        )

    def summarize(self, exchange: Exchange, summary: str, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Store one procedural summary of how a task was carried out.

        Args:
            exchange: Turns the summary was written from.
            summary: The summary a model would have produced.
            at: ISO 8601 time recorded on the write.
            run: Run to write in, defaulting to the bound one.

        Returns:
            The single `ADD` the write produced.
        """

        return self._call(
            "add_memories",
            {
                "messages": [message.as_json() for message in exchange.messages],
                "facts": [{"text": summary}],
                "memory_type": PROCEDURAL,
                "at": at,
                "run": run,
            },
        )

    def revise(self, memory_id: str, text: str, *, at: str, actor: str | None = None) -> dict[str, Any]:
        """Correct one memory directly, without asking the pipeline.

        Args:
            memory_id: Memory to rewrite.
            text: Replacement text.
            at: ISO 8601 time recorded on the write.
            actor: Who asked for the correction.

        Returns:
            The change that was applied.
        """

        return self._call("update_memory", {"memory_id": memory_id, "text": text, "at": at, "actor": actor})

    def discard(self, memory_id: str, *, at: str, actor: str | None = None) -> dict[str, Any]:
        """Delete one memory because the caller asked for it to go.

        Args:
            memory_id: Memory to delete.
            at: ISO 8601 time recorded on the history row.
            actor: Who asked for the deletion.

        Returns:
            The change that was applied, carrying the deleted memory.
        """

        return self._call("delete_memory", {"memory_id": memory_id, "at": at, "actor": actor})

    def forget(self, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Retire one scope and everything stored below it.

        Args:
            at: ISO 8601 time recorded on every history row.
            run: Run to retire, or `None` to retire the whole bound scope.

        Returns:
            The memories that were removed.
        """

        return self._call("forget_scope", {"at": at, "run": run})

    def inspect(self, memory_id: str) -> dict[str, Any]:
        """Read every recorded change to one memory.

        Args:
            memory_id: Memory to read the history of.

        Returns:
            The events, and whether the memory itself still exists.
        """

        return self._call("memory_history", {"memory_id": memory_id})

    def answer(self, question: str, *, query: str, memory_id: str, limit: int = 3, threshold: float = 0.0) -> str:
        """Answer from the memories, the relationships, and one memory's past.

        Args:
            question: User-facing question included in the report.
            query: Retrieval text for the search.
            memory_id: Memory whose history is shown, to make the pipeline's
                decisions visible in the answer.
            limit: Maximum search hits to show.
            threshold: Minimum score a hit must reach.

        Returns:
            Readable answer assembled from tool results.
        """

        found = self._call(
            "search_memories",
            {"query": query, "limit": limit, "threshold": threshold, "filters": {"memory_type": FACTUAL}},
        )
        procedure = self._call("list_memories", {"filters": {"memory_type": PROCEDURAL}, "limit": limit})
        past = self._call("memory_history", {"memory_id": memory_id})
        lines = [
            f"Question: {question}",
            f"Relevant memories for {query!r}:",
            *_memory_lines(found["results"]),
            "Related entities:",
            *_relation_lines(found["relations"]),
            "How this was handled before:",
            *_memory_lines(procedure["results"]),
            f"How the top memory reached its current wording (v{_version(past['events'])}):",
            *_history_lines(past["events"]),
        ]
        return "\n".join(lines)

    def tool_usage(self) -> dict[str, int]:
        """Return call counts per tool in first-call order.

        Returns:
            Tool names mapped to how often the agent called them.
        """

        return dict(Counter(record.name for record in self.call_log))

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and retain an isolated trace record."""

        result = self.tools.call(name, {key: value for key, value in arguments.items() if value is not None})
        self.call_log.append(ToolCallRecord(name=name, arguments=deepcopy(arguments), result=deepcopy(result)))
        return result


def _definitions(session: Session, client: Mem0Memory) -> tuple[ToolDefinition, ...]:
    """Describe the tool set, including the scope every call is made inside."""

    bound = "/".join(session.path[:2])
    floor = client.updater.threshold
    return (
        ToolDefinition(
            name="add_memories",
            description=(
                f"Extract memories from an exchange and reconcile them against {bound}. "
                f"Each candidate becomes ADD, UPDATE, DELETE, or NONE at a {floor} similarity floor."
            ),
            parameters={
                "messages": "list of {role, content, name}",
                "facts": "list of {text, retracts} a model would have extracted",
                "relations": "list of {source, relationship, destination, retracts}",
                "infer": "false stores the turns verbatim instead",
                "memory_type": "factual or procedural_memory",
                "metadata": "optional metadata kept and indexed with each memory",
                "at": "ISO 8601 timestamp",
                "run": "optional run inside the bound scope",
                "actor": "optional speaker to credit",
            },
        ),
        ToolDefinition(
            name="search_memories",
            description=f"Search memories below {bound} and return the relationships beside them.",
            parameters={
                "query": "string",
                "filters": "optional metadata filter, such as {'memory_type': 'factual'}",
                "limit": "integer",
                "threshold": "minimum score, from zero to one",
                "run": "optional run inside the bound scope",
            },
        ),
        ToolDefinition(
            name="list_memories",
            description=f"List every memory below {bound}, or below one run inside it.",
            parameters={
                "filters": "optional metadata filter",
                "limit": "integer",
                "run": "optional run inside the bound scope",
            },
        ),
        ToolDefinition(
            name="update_memory",
            description="Rewrite one memory by id, keeping the id and recording the old wording.",
            parameters={
                "memory_id": "string",
                "text": "replacement memory",
                "metadata": "optional metadata to merge",
                "at": "ISO 8601 timestamp",
                "actor": "optional speaker to credit",
            },
        ),
        ToolDefinition(
            name="delete_memory",
            description="Delete one memory by id. Its recorded history stays readable.",
            parameters={"memory_id": "string", "at": "ISO 8601 timestamp", "actor": "optional speaker to credit"},
        ),
        ToolDefinition(
            name="memory_history",
            description="Read every ADD, UPDATE, and DELETE recorded for one memory.",
            parameters={"memory_id": "string"},
        ),
        ToolDefinition(
            name="forget_scope",
            description=f"Retire {bound}, or one run inside it, with everything stored below it.",
            parameters={"at": "ISO 8601 timestamp", "run": "optional run inside the bound scope"},
        ),
    )


def _memory_lines(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render stored memories for the agent's answer, or say there were none."""

    if not results:
        return ["- none"]
    return [f"- {result['memory']} (v{result['version']}{_score(result)})" for result in results]


def _relation_lines(relations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render relationships for the agent's answer, or say there were none."""

    if not relations:
        return ["- none"]
    return [f"- {relation['source']} {relation['relationship']} {relation['destination']}" for relation in relations]


def _history_lines(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render one memory's recorded changes for the agent's answer."""

    if not events:
        return ["- none"]
    return [
        f"- {event['event']}: {event['old_memory'] or 'nothing on file'} -> {event['new_memory'] or 'deleted'}"
        for event in events
    ]


def _score(result: Mapping[str, Any]) -> str:
    """Render a hit's score, when the result carries one."""

    return "" if "score" not in result else f", score {result['score']}"


def _version(events: Sequence[Mapping[str, Any]]) -> int:
    """Return how many recorded changes a memory has."""

    return len(events)
