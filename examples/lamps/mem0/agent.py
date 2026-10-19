"""A tool interface and a deterministic agent for the Mem0 lamp.

Agents use Mem0 through a small set of calls. The important property of these
calls is that the caller has little control:

- **The pipeline decides:** an agent gives an exchange. The pipeline decides
  what to store, replace, or remove. The agent reads the returned events after
  the call to find the result.
- **Tools return decisions:** because of this asymmetry, the tools here return
  the decisions, not a success flag. The call log shows the memory writes of
  the agent.
- **Bound scope:** the tool set gets the user and agent identifiers once, at
  construction. This is the bound scope. No tool argument can name the memories
  of a different user.
- **Id check in the tools:** the client below keeps one flat id space, as
  upstream does. Thus the 3 tools that take a memory id check that the id is in
  the bound scope before they act. This check is in this layer and not in the
  client, because a model supplies the arguments to this layer.
- **Run as an argument:** a run is a tool argument, because a run is one
  conversation and upstream gives it for each call. A run can only make the
  bound scope narrower. It cannot go out of the bound scope.
- **No model:** the example agent selects its calls from explicit inputs, not
  from a model. Thus the demo runs with no API key. Upstream, extraction and the
  decision to add, replace, or forget are the 2 steps that really need a model.
  Thus the agent receives candidate facts and relationships that are already
  formed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from examples.lamps.mem0.client import Mem0Memory
from examples.lamps.mem0.model import (
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
    """Description of a tool for an agent.

    Attributes:
        name: Stable tool name for calls.
        description: Plain-language guidance for the agent.
        parameters: Map from argument names to short type descriptions.
    """

    name: str
    description: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call by the example agent, for later inspection.

    Attributes:
        name: Name of the called tool.
        arguments: Copy of the JSON-compatible arguments.
        result: Copy of the JSON-compatible result.
    """

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class Mem0Tools:
    """Memory tools with a bound scope and JSON-compatible arguments."""

    def __init__(self, client: Mem0Memory, *, session: Session) -> None:
        """Bind one tool set to one user and agent.

        Args:
            client: Memory client that the tools call.
            session: Scope of all calls. Usually it names only the user and
                the agent. A `run_id` on the session becomes the default run,
                and a call can replace it. No tool argument can name the user
                or the agent. The tools refuse a memory id from outside this
                scope in the same way as an unknown id.
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
            raise ValueError(f"Unknown Mem0 tool {name!r}.")
        return handler(**arguments)

    def scope(self, *, run: str | None = None, actor: str | None = None) -> Session:
        """Return the session for one call.

        Args:
            run: Run that makes the scope narrower, or `None` to keep the bound
                run.
            actor: Speaker to record as the actor, or `None` to keep the bound
                actor.

        Returns:
            A session in the bound user and agent.
        """

        selected = optional_text(run, field="run")
        return Session(
            user_id=self.session.user_id,
            agent_id=self.session.agent_id,
            run_id=selected or self.session.run_id,
            actor_id=optional_text(actor, field="actor") or self.session.actor_id,
        )

    def owns(self, memory_id: str) -> bool:
        """Return `True` if one memory id is in the bound scope.

        The method does a prefix test on the bound path. Thus a tool set for a
        user and an agent reaches all runs below them, and nothing beside them.

        Args:
            memory_id: Id from a tool argument.

        Returns:
            `True` if the client wrote a memory with that id in the bound scope.
            The result is also `True` if the client deleted that memory later.
        """

        bound = self.session.path
        owner = self.client.owner(required_text(memory_id, field="memory_id"))
        return owner is not None and owner[: len(bound)] == bound

    def _require_own(self, memory_id: str) -> str:
        """Return the id, or refuse it in the same way as an unknown id.

        An id outside the bound scope and an id with no writes cause the same
        `KeyError`. Thus a caller cannot use a tool set to find which ids exist
        in a different scope.

        Args:
            memory_id: Id from a tool argument.

        Returns:
            The id, if it is in the bound scope.

        Raises:
            KeyError: If the id is unknown or belongs to a different scope.
        """

        if not self.owns(memory_id):
            raise KeyError(f"Memory {memory_id!r} was not found.")
        return memory_id

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
        """Implement `add_memories`, the full write with 2 phases."""

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
        """Implement `update_memory`, the correction from the caller."""

        change = self.client.update(
            self._require_own(memory_id),
            text,
            metadata=json_mapping(metadata, field="metadata") or None,
            actor_id=optional_text(actor, field="actor"),
            at=parse_timestamp(at, field="at"),
        )
        return change.as_json()

    def _delete_memory(self, *, memory_id: str, at: str, actor: str | None = None) -> dict[str, Any]:
        """Implement `delete_memory`, the removal from the caller."""

        change = self.client.delete(
            self._require_own(memory_id),
            actor_id=optional_text(actor, field="actor"),
            at=parse_timestamp(at, field="at"),
        )
        return change.as_json()

    def _memory_history(self, *, memory_id: str) -> dict[str, Any]:
        """Implement `memory_history` for one memory, also if the memory is deleted.

        An id from a different scope gives the same result as an id with no
        writes. This tool gives the same result for an unknown id.
        """

        if not self.owns(memory_id):
            return {"memory_id": memory_id, "exists": False, "events": []}
        entries = self.client.history(memory_id)
        return {
            "memory_id": memory_id,
            "exists": self.client.get(memory_id) is not None,
            "events": [entry.as_json() for entry in entries],
        }

    def _forget_scope(self, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Implement `forget_scope`, one subtree delete for a full scope."""

        scope = self.scope(run=run)
        changes = self.client.delete_all(scope, at=parse_timestamp(at, field="at"))
        return {"scope": scope.label, "removed": [change.as_json() for change in changes]}


@dataclass(slots=True)
class Mem0Agent:
    """A deterministic agent that uses memory only through the tool set.

    Attributes:
        name: Agent name in the demo output.
        tools: Tool set with a bound scope.
        call_log: All calls of the agent, in order.
    """

    name: str
    tools: Mem0Tools
    call_log: list[ToolCallRecord] = field(default_factory=list)

    def learn(self, exchange: Exchange, *, at: str, run: str | None = None, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Give one exchange to the pipeline and read its decisions.

        Args:
            exchange: Turns, candidate facts, and candidate relationships.
            at: ISO 8601 time that the call records on each write.
            run: Run to write in. The default is the bound run.
            metadata: Application metadata to keep with each memory.

        Returns:
            The memory events and relationship changes of the call.
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
        """Store an exchange in its original words, with no extraction.

        Args:
            exchange: Turns to keep in their original words.
            at: ISO 8601 time that the call records on the writes.
            run: Run to write in. The default is the bound run.

        Returns:
            One `ADD` for each turn that the extractor reads.
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
        """Store one procedural summary of the steps of a task.

        Args:
            exchange: Turns that the summary comes from.
            summary: The summary, as a model would write it.
            at: ISO 8601 time that the call records on the write.
            run: Run to write in. The default is the bound run.

        Returns:
            The single `ADD` of the write.
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
        """Correct one memory directly, without the pipeline.

        Args:
            memory_id: Memory to rewrite.
            text: New text.
            at: ISO 8601 time that the call records on the write.
            actor: Speaker that asked for the correction.

        Returns:
            The applied change.
        """

        return self._call("update_memory", {"memory_id": memory_id, "text": text, "at": at, "actor": actor})

    def discard(self, memory_id: str, *, at: str, actor: str | None = None) -> dict[str, Any]:
        """Delete one memory because the caller asks for the delete.

        Args:
            memory_id: Memory to delete.
            at: ISO 8601 time that the call records on the history row.
            actor: Speaker that asked for the delete.

        Returns:
            The applied change, with the deleted memory.
        """

        return self._call("delete_memory", {"memory_id": memory_id, "at": at, "actor": actor})

    def forget(self, *, at: str, run: str | None = None) -> dict[str, Any]:
        """Retire one scope and all memories below it.

        Args:
            at: ISO 8601 time that the call records on each history row.
            run: Run to retire, or `None` to retire the full bound scope.

        Returns:
            The scope label, and one change for each removed memory.
        """

        return self._call("forget_scope", {"at": at, "run": run})

    def inspect(self, memory_id: str) -> dict[str, Any]:
        """Read all recorded changes to one memory.

        Args:
            memory_id: Memory to read the history of.

        Returns:
            The events, and a flag that tells if the memory still exists.
        """

        return self._call("memory_history", {"memory_id": memory_id})

    def answer(self, question: str, *, query: str, memory_id: str, limit: int = 3, threshold: float = 0.0) -> str:
        """Answer from the memories, the relationships, the procedures, and the history of one memory.

        Args:
            question: Question for the user, in the report.
            query: Search text for the retrieval.
            memory_id: Memory whose history the answer shows. The history makes
                the decisions of the pipeline visible in the answer.
            limit: Maximum number of search hits, and of procedural memories,
                to show.
            threshold: Minimum score of a hit.

        Returns:
            Readable answer from the tool results.
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
        """Return the number of calls for each tool, in the order of the first call.

        Returns:
            Map from tool names to the number of calls by the agent.
        """

        return dict(Counter(record.name for record in self.call_log))

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and keep a separate trace record."""

        result = self.tools.call(name, {key: value for key, value in arguments.items() if value is not None})
        self.call_log.append(ToolCallRecord(name=name, arguments=deepcopy(arguments), result=deepcopy(result)))
        return result


def _definitions(session: Session, client: Mem0Memory) -> tuple[ToolDefinition, ...]:
    """Describe the tool set, with the scope of all calls."""

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
    """Format the stored memories for the agent answer, or return a `- none` line if there are none."""

    if not results:
        return ["- none"]
    return [f"- {result['memory']} (v{result['version']}{_score(result)})" for result in results]


def _relation_lines(relations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Format the relationships for the agent answer, or return a `- none` line if there are none."""

    if not relations:
        return ["- none"]
    return [f"- {relation['source']} {relation['relationship']} {relation['destination']}" for relation in relations]


def _history_lines(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Format the recorded changes of one memory for the agent answer, or return a `- none` line if there are none."""

    if not events:
        return ["- none"]
    return [
        f"- {event['event']}: {event['old_memory'] or 'nothing on file'} -> {event['new_memory'] or 'deleted'}"
        for event in events
    ]


def _score(result: Mapping[str, Any]) -> str:
    """Format the score of a hit, if the result has a score."""

    return "" if "score" not in result else f", score {result['score']}"


def _version(events: Sequence[Mapping[str, Any]]) -> int:
    """Return the number of recorded changes of a memory."""

    return len(events)
