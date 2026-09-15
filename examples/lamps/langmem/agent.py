"""A tool interface and deterministic agent for the LangMem lamp.

LangMem gives an agent two ways to write memory and expects both to be used:

- The hot path is a tool the agent calls mid-conversation, when it notices
  something worth keeping.
- The background path is a queued reflection that reads the whole exchange
  afterwards and proposes changes to what is already on file.

They are different tools with different guardrails. The hot-path tool is
restricted by `actions_permitted`, the manager by `enable_inserts` and its
siblings. This module keeps both gates, so the difference is visible in the call
log.

- Every tool is bound to a namespace template resolved once, at construction,
  from request-scoped values. So no tool argument names a user, and a tool set
  built for one user cannot address another's memories.
- The agent decides which tools to call from explicit inputs rather than from a
  model, which keeps the example runnable with no API key. Extraction and prompt
  rewriting are the two steps that genuinely need a model upstream, so the agent
  receives proposed changes and keyed feedback rules already formed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from examples.lamps.langmem.manager import MemoryManager
from examples.lamps.langmem.model import (
    AnnotatedTrajectory,
    Conversation,
    ManagerRun,
    MemoryItem,
    Namespace,
    PatchOutcome,
    Profile,
    PromptRule,
    SearchHit,
    format_timestamp,
    json_mapping,
    messages_from_json,
    namespace_path,
    parse_timestamp,
    patch_as_json,
    patch_from_json,
    required_text,
    resolve_namespace,
    unique_strings,
)
from examples.lamps.langmem.profiles import ProfileStore
from examples.lamps.langmem.prompts import PromptOptimizer
from examples.lamps.langmem.reflection import ReflectionQueue
from examples.lamps.langmem.store import MemoryStore

TOOL_ACTIONS: tuple[str, ...] = ("create", "update", "delete")


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


class LangMemTools:
    """Namespace-bound memory tools with JSON-compatible arguments."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        profiles: ProfileStore,
        manager: MemoryManager,
        queue: ReflectionQueue,
        optimizer: PromptOptimizer,
        namespace: Sequence[str],
        context: Mapping[str, str],
        actions_permitted: Sequence[str] = TOOL_ACTIONS,
    ) -> None:
        """Bind one tool set to one resolved namespace.

        Args:
            store: Collection-shaped memory.
            profiles: Profile-shaped memory.
            manager: Manager the queue runs against due submissions.
            queue: Deferred reflection queue.
            optimizer: Procedural memory for the agent's prompts.
            namespace: Namespace template, whose `{placeholder}` segments are
                filled from `context`.
            context: Request-scoped values the template is resolved with.
            actions_permitted: Hot-path actions this tool set allows, from
                `create`, `update`, and `delete`.

        Raises:
            KeyError: If the template has a placeholder `context` does not fill.
            ValueError: If an action outside the three names is permitted.
        """

        self.store = store
        self.profiles = profiles
        self.manager = manager
        self.queue = queue
        self.optimizer = optimizer
        self.namespace: Namespace = resolve_namespace(namespace, context)
        permitted = unique_strings(actions_permitted, field="actions_permitted")
        unknown = set(permitted) - set(TOOL_ACTIONS)
        if unknown:
            raise ValueError(f"Unknown permitted actions: {', '.join(sorted(unknown))}.")
        self.actions_permitted = permitted
        self.definitions = _definitions(self.namespace, permitted, manager)
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "manage_memory": self._manage_memory,
            "search_memory": self._search_memory,
            "submit_conversation": self._submit_conversation,
            "run_reflection": self._run_reflection,
            "get_profile": self._get_profile,
            "optimize_prompt": self._optimize_prompt,
            "get_prompt": self._get_prompt,
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
            raise ValueError(f"Unknown LangMem tool {name!r}.")
        return handler(**arguments)

    def _manage_memory(
        self,
        *,
        action: str,
        collection: str,
        key: str,
        value: Any | None = None,
    ) -> dict[str, Any]:
        """Implement `manage_memory`, the hot-path write."""

        if action not in TOOL_ACTIONS:
            raise ValueError(f"action must be one of {list(TOOL_ACTIONS)}.")
        if action not in self.actions_permitted:
            return _refused(action, collection, key, f"{action} is not permitted for this tool")
        target = (*self.namespace, required_text(collection, field="collection"))
        existing = self.store.get(target, key)
        if action == "create" and existing is not None:
            return _refused(action, collection, key, "a memory is already stored at that address")
        if action != "create" and existing is None:
            return _refused(action, collection, key, "nothing is stored at that address")
        if action == "delete":
            return {"action": action, "applied": True, "item": _item(self.store.delete(target, key))}
        if value is None:
            raise ValueError(f"value is required when action is {action!r}.")
        item = (
            self.store.insert(target, key, value)
            if action == "create"
            else self.store.update(target, key, value)
        )
        return {"action": action, "applied": True, "item": _item(item)}

    def _search_memory(
        self,
        *,
        query: str,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Implement `search_memory` below the bound namespace."""

        hits = self.store.search(
            self.namespace,
            query,
            filters=json_mapping(filter, field="filter") or None,
            limit=limit,
            offset=offset,
        )
        return {
            "query": query,
            "filter": json_mapping(filter, field="filter"),
            "limit": limit,
            "offset": offset,
            "results": [_hit(hit) for hit in hits],
        }

    def _submit_conversation(
        self,
        *,
        thread: str,
        messages: Sequence[Mapping[str, Any]],
        at: str,
        after_seconds: int = 0,
        patches: Sequence[Mapping[str, Any]] = (),
        profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement `submit_conversation`, the deferred write."""

        submission = self.queue.submit(
            Conversation(
                thread=required_text(thread, field="thread"),
                messages=messages_from_json(messages),
                patches=tuple(patch_from_json(entry) for entry in patches),
                profile=json_mapping(profile, field="profile"),
            ),
            base=self.namespace,
            at=parse_timestamp(at, field="at"),
            after_seconds=after_seconds,
        )
        superseded = next(
            (
                entry
                for entry in self.queue.history()
                if entry.superseded_by == submission.block.id
            ),
            None,
        )
        return {
            "thread": submission.thread,
            "sequence": submission.sequence,
            "state": submission.state,
            "due_at": format_timestamp(submission.due_at),
            "messages": len(submission.messages),
            "patches": [patch_as_json(patch) for patch in submission.patches],
            "superseded": None if superseded is None else {"sequence": superseded.sequence, "thread": superseded.thread},
        }

    def _run_reflection(self, *, now: str) -> dict[str, Any]:
        """Implement `run_reflection` over the submissions that are due."""

        moment = parse_timestamp(now, field="now")
        runs = self.queue.run_due(now=moment)
        return {
            "now": format_timestamp(moment),
            "runs": [_run(run) for run in runs],
            "still_pending": [
                {"thread": submission.thread, "due_at": format_timestamp(submission.due_at)}
                for submission in self.queue.pending()
            ],
        }

    def _get_profile(self) -> dict[str, Any]:
        """Implement `get_profile` for the bound namespace."""

        name = self.manager.profile_name
        current = self.profiles.current(self.namespace, name)
        if current is None:
            return {"name": name, "namespace": namespace_path(self.namespace), "revision": 0, "value": {}, "previous_versions": []}
        history = self.profiles.history(self.namespace, name)
        return {
            "name": current.name,
            "namespace": namespace_path(current.namespace),
            "revision": current.revision,
            "value": deepcopy(current.value),
            "previous_versions": [
                {"revision": version.revision, "value": deepcopy(version.value)}
                for version in history
                if version.revision < current.revision
            ],
        }

    def _optimize_prompt(
        self,
        *,
        name: str,
        trajectories: Sequence[Mapping[str, Any]],
        at: str,
    ) -> dict[str, Any]:
        """Implement `optimize_prompt` over annotated past exchanges."""

        before = self.optimizer.current(name)
        version = self.optimizer.optimize(
            name,
            [_trajectory(entry) for entry in trajectories],
            at=parse_timestamp(at, field="at"),
        )
        return {
            "name": version.name,
            "revision": version.revision,
            "changed": before is None or version.revision != before.revision,
            "text": version.text,
            "rules": [{"key": rule.key, "text": rule.text} for rule in version.rules],
            "sources": list(version.sources),
        }

    def _get_prompt(self, *, name: str) -> dict[str, Any]:
        """Implement `get_prompt` and report the versions behind it."""

        current = self.optimizer.current(name)
        if current is None:
            raise ValueError(f"Unknown prompt {name!r}.")
        return {
            "name": current.name,
            "revision": current.revision,
            "text": current.text,
            "when_to_update": current.when_to_update,
            "rules": [{"key": rule.key, "text": rule.text} for rule in current.rules],
            "sources": list(current.sources),
            "previous_versions": [
                {"revision": version.revision, "rules": [rule.key for rule in version.rules]}
                for version in self.optimizer.history(name)
                if version.revision < current.revision
            ],
        }


class LangMemAgent:
    """Deterministic agent whose only memory access is the tool set."""

    def __init__(self, *, name: str, tools: LangMemTools) -> None:
        """Create an agent over one bound tool set.

        Args:
            name: Agent name shown in the demo.
            tools: Namespace-bound tool set.
        """

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self.call_log: list[ToolCallRecord] = []

    def submit(
        self,
        conversation: Conversation,
        *,
        at: str,
        after_seconds: int = 0,
    ) -> dict[str, Any]:
        """Hand a conversation to the reflection queue.

        Args:
            conversation: `Exchange` and the changes proposed from it.
            at: ISO 8601 time the exchange was handed over.
            after_seconds: Debounce window before the manager may run it.

        Returns:
            The queued submission, naming anything it superseded.
        """

        return self._call(
            "submit_conversation",
            {
                "thread": conversation.thread,
                "messages": [message.as_json() for message in conversation.messages],
                "at": at,
                "after_seconds": after_seconds,
                "patches": [patch_as_json(patch) for patch in conversation.patches],
                "profile": deepcopy(conversation.profile),
            },
        )

    def reflect(self, *, now: str) -> dict[str, Any]:
        """Run the manager over every submission that has come due.

        Args:
            now: ISO 8601 current time.

        Returns:
            What each run applied, refused, and left pending.
        """

        return self._call("run_reflection", {"now": now})

    def remember(
        self,
        *,
        action: str,
        collection: str,
        key: str,
        value: Any | None = None,
    ) -> dict[str, Any]:
        """Write one memory on the hot path, mid-conversation.

        Args:
            action: `create`, `update`, or `delete`.
            collection: Namespace segment below the bound namespace.
            key: Key inside that namespace.
            value: Value for a create or an update.

        Returns:
            The stored item, or the refusal if the tool does not permit the
            action.
        """

        arguments: dict[str, Any] = {"action": action, "collection": collection, "key": key}
        if value is not None:
            arguments["value"] = deepcopy(value)
        return self._call("manage_memory", arguments)

    def improve(
        self,
        *,
        prompt: str,
        trajectories: Sequence[AnnotatedTrajectory],
        at: str,
    ) -> dict[str, Any]:
        """Fold feedback on past exchanges into the agent's own instructions.

        Args:
            prompt: Registered prompt name.
            trajectories: Past exchanges with keyed feedback rules.
            at: ISO 8601 time recorded on a revision, if one is written.

        Returns:
            The prompt after the optimization, and whether it changed.
        """

        return self._call(
            "optimize_prompt",
            {
                "name": prompt,
                "trajectories": [
                    {
                        "name": trajectory.name,
                        "messages": [message.as_json() for message in trajectory.messages],
                        "feedback": trajectory.feedback,
                        "rules": [{"key": rule.key, "text": rule.text} for rule in trajectory.rules],
                    }
                    for trajectory in trajectories
                ],
                "at": at,
            },
        )

    def answer(
        self,
        question: str,
        *,
        prompt: str,
        query: str,
        schema: str,
        example_schema: str,
        limit: int = 2,
    ) -> str:
        """Answer using procedural, profile, semantic, and episodic memory.

        Args:
            question: User-facing question included in the report.
            prompt: Prompt whose current version governs the answer.
            query: Retrieval text for both searches.
            schema: Schema filter for the semantic search.
            example_schema: Schema filter for the episodic search.
            limit: Semantic hits to show.

        Returns:
            Readable answer assembled from tool results.
        """

        instructions = self._call("get_prompt", {"name": prompt})
        profile = self._call("get_profile", {})
        facts = self._call(
            "search_memory",
            {"query": query, "filter": {"schema": schema}, "limit": limit, "offset": 0},
        )
        examples = self._call(
            "search_memory",
            {"query": query, "filter": {"schema": example_schema}, "limit": 1, "offset": 0},
        )
        credit = ", ".join(instructions["sources"]) or "no feedback yet"
        lines = [
            f"Question: {question}",
            f"Procedural memory ({instructions['name']} r{instructions['revision']}, from {credit}):",
            *[f"  {line}" for line in instructions["text"].splitlines()],
            f"Profile {profile['name']} r{profile['revision']}: {_fields(profile['value'])}",
            *[f"- was r{version['revision']}: {_fields(version['value'])}" for version in profile["previous_versions"]],
            f"Relevant memories for {query!r}:",
            *_result_lines(facts["results"]),
            "Episode to follow:",
            *_result_lines(examples["results"]),
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

        result = self._toolbox.call(name, arguments)
        self.call_log.append(
            ToolCallRecord(
                name=name,
                arguments=deepcopy(arguments),
                result=deepcopy(result),
            )
        )
        return result


def _definitions(
    namespace: Namespace,
    actions_permitted: Sequence[str],
    manager: MemoryManager,
) -> tuple[ToolDefinition, ...]:
    """Describe the tool set, including the guardrails on each write path."""

    permitted = " | ".join(actions_permitted)
    enabled = ", ".join(f"{action}={str(allowed).lower()}" for action, allowed in manager.permissions.items())
    return (
        ToolDefinition(
            name="manage_memory",
            description=f"Write one memory now, under {namespace_path(namespace)}. Permitted: {permitted}.",
            parameters={
                "action": permitted,
                "collection": "namespace segment, such as semantic or episodes",
                "key": "string",
                "value": "schema-shaped object; omitted for delete",
            },
        ),
        ToolDefinition(
            name="search_memory",
            description=f"Search memories below {namespace_path(namespace)} by text and metadata.",
            parameters={
                "query": "string",
                "filter": "optional metadata filter, such as {'schema': 'semantic_fact'}",
                "limit": "integer",
                "offset": "integer",
            },
        ),
        ToolDefinition(
            name="submit_conversation",
            description="Queue a conversation for background memory formation, debounced per thread.",
            parameters={
                "thread": "string",
                "messages": "list of {role, content}",
                "at": "ISO 8601 timestamp",
                "after_seconds": "integer delay before the manager may run",
                "patches": "list of {action, collection, key, value}",
                "profile": "optional profile fields to merge",
            },
        ),
        ToolDefinition(
            name="run_reflection",
            description=f"Apply every submission that has come due. Manager permissions: {enabled}.",
            parameters={"now": "ISO 8601 timestamp"},
        ),
        ToolDefinition(
            name="get_profile",
            description=f"Read the single {manager.profile_name!r} document and the versions before it.",
            parameters={},
        ),
        ToolDefinition(
            name="optimize_prompt",
            description="Fold feedback on past exchanges into a prompt, if it changes the rules.",
            parameters={
                "name": "string",
                "trajectories": "list of {name, messages, feedback, rules}",
                "at": "ISO 8601 timestamp",
            },
        ),
        ToolDefinition(
            name="get_prompt",
            description="Read the prompt the agent runs with, its rules, and its earlier versions.",
            parameters={"name": "string"},
        ),
    )


def _trajectory(payload: Mapping[str, Any]) -> AnnotatedTrajectory:
    """Build one annotated exchange from JSON-compatible tool arguments."""

    return AnnotatedTrajectory(
        name=required_text(payload.get("name"), field="name"),
        messages=messages_from_json(payload.get("messages", ())),
        feedback=required_text(payload.get("feedback"), field="feedback"),
        rules=tuple(
            PromptRule(
                key=required_text(entry.get("key"), field="rule key"),
                text=required_text(entry.get("text"), field="rule text"),
            )
            for entry in payload.get("rules", ())
        ),
    )


def _item(item: MemoryItem) -> dict[str, Any]:
    """Return one stored memory as a JSON-compatible result."""

    return {
        "id": item.id,
        "address": item.address,
        "version": item.version,
        "schema": item.schema_name,
        "value": deepcopy(item.value),
    }


def _hit(hit: SearchHit) -> dict[str, Any]:
    """Return one search hit as a JSON-compatible result."""

    return {"item": _item(hit.item), "score": hit.score, "matched": list(hit.matched)}


def _outcome(outcome: PatchOutcome) -> dict[str, Any]:
    """Return one applied or refused change as a JSON-compatible result."""

    return {
        "proposed": outcome.patch.action,
        "action": outcome.action,
        "address": f"{outcome.patch.collection}/{outcome.patch.key}",
        "reason": outcome.reason,
        "version": None if outcome.item is None else outcome.item.version,
    }


def _run(run: ManagerRun) -> dict[str, Any]:
    """Return one manager run as a JSON-compatible result."""

    return {
        "thread": run.thread,
        "source_id": run.source_id,
        "applied": [_outcome(outcome) for outcome in run.applied],
        "refused": [_outcome(outcome) for outcome in run.refused],
        "profile": None if run.profile is None else _profile(run.profile),
        "profile_refused": run.profile_reason,
    }


def _profile(profile: Profile) -> dict[str, Any]:
    """Return one profile version as a JSON-compatible result."""

    return {"revision": profile.revision, "value": deepcopy(profile.value)}


def _fields(value: Mapping[str, Any]) -> str:
    """Render a profile value as a compact key list."""

    return ", ".join(f"{key}={value[key]}" for key in sorted(value)) or "empty"


def _result_lines(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render search hits for the agent's answer, or say there were none."""

    if not results:
        return ["- none"]
    return [
        f"- {result['item']['address']} v{result['item']['version']} "
        f"(score {result['score']}): {_text(result['item']['value'])}"
        for result in results
    ]


def _text(value: Any) -> str:
    """Render a stored value as one readable line."""

    if isinstance(value, Mapping):
        return " ".join(str(value[key]) for key in value)
    return str(value)


def _refused(action: str, collection: str, key: str, reason: str) -> dict[str, Any]:
    """Return the result of a hot-path write the tool would not make."""

    return {
        "action": action,
        "applied": False,
        "reason": reason,
        "address": f"{collection}/{key}",
    }
