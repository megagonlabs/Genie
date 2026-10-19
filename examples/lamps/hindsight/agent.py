"""A tool interface and a deterministic agent for the Hindsight lamp.

Hindsight is a service that an agent calls. It retains payloads, consolidates
them in the background, keeps standing answers current, and serves recall and
reflection. This module makes that surface available as named tools with
JSON-compatible arguments and results. So an application can give the same
definitions to a function-calling loop, and the bank below does not change.

The example uses 2 deliberate simplifications, so it runs without an API key:

- **No model decisions:** the agent selects the tools to call from explicit
  inputs, not from a model. It also gets facts that are already extracted.
  Upstream, fact extraction is the step that really needs a language model.
- **Explicit consolidation:** upstream runs consolidation automatically after
  every `retain`. Here it is an explicit call, so the example can show when it
  happened.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from examples.lamps.hindsight.bank import HindsightMemoryBank
from examples.lamps.hindsight.mental_models import MentalModelStore
from examples.lamps.hindsight.model import (
    Claim,
    Evidence,
    FactDraft,
    FactType,
    MemoryFact,
    MentalModel,
    Observation,
    Reflection,
    format_timestamp,
    parse_timestamp,
)
from examples.lamps.hindsight.observations import ConsolidationEngine
from examples.lamps.hindsight.recall import HindsightRecall, RecallResponse, RecallWindow
from examples.lamps.hindsight.reflect import Reflector


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Description of a tool that the agent gets.

    Attributes:
        name: Stable tool name for calls.
        description: Plain-language guidance for the agent.
        parameters: Map of argument names to short type descriptions.
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


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    """A canonical entity that the agent declares before it retains a payload.

    Attributes:
        name: Canonical display name.
        kind: Application-defined entity kind.
        aliases: Extra surface forms to bind to the same entity.
    """

    name: str
    kind: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """One fact that an extractor took from a document.

    Attributes:
        key: Agent-local name, so links and corrections can refer to the fact.
        text: Narrative sentence to store.
        fact_type: `world` or `experience`.
        context: Situation that the fact came from.
        entities: Entity names as written in the source.
        occurred_at: Optional ISO 8601 start of the described period.
        occurred_until: Optional ISO 8601 exclusive end of that period.
        tags: Visibility tags.
        claim: Optional key of the belief that this fact is evidence for.
        belief: Sentence that the claim states. The `retain` tool raises
            `ValueError` if only one of `claim` and `belief` is set.
        stance: `supports` or `contradicts`, for that belief.
    """

    key: str
    text: str
    fact_type: FactType
    context: str = ""
    entities: tuple[str, ...] = ()
    occurred_at: str | None = None
    occurred_until: str | None = None
    tags: tuple[str, ...] = ()
    claim: str | None = None
    belief: str | None = None
    stance: str = "supports"


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    """A temporal, semantic, or causal link between two retained facts.

    Attributes:
        source_key: Key of the fact where the directed link starts.
        target_key: Key of the fact where the directed link ends.
        relation: One of the associative relations of the lamp.
        metadata: Optional link properties.
    """

    source_key: str
    target_key: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentCandidate:
    """One source payload and everything the agent does with it.

    Attributes:
        source: Where the payload came from.
        content: Raw payload text.
        retained_at: ISO 8601 time when the bank learned the payload.
        context: Optional text that describes the situation.
        facts: Facts extracted from the payload.
        links: Associative links between facts of this payload or of earlier
            payloads.
        corrections: Keys of earlier facts that this payload retires.
    """

    source: str
    content: str
    retained_at: str
    context: str = ""
    facts: tuple[FactCandidate, ...] = ()
    links: tuple[LinkCandidate, ...] = ()
    corrections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """A standing question that the agent wants a current answer for.

    Attributes:
        name: Stable model name.
        question: Question that the model answers.
        tags: Scope tags that select the observations that the model reads.
    """

    name: str
    question: str
    tags: tuple[str, ...] = ()


class HindsightTools:
    """Make the operations of one memory bank available as named agent tools."""

    def __init__(
        self,
        bank: HindsightMemoryBank,
        *,
        consolidation: ConsolidationEngine,
        models: MentalModelStore,
        recall: HindsightRecall,
        reflector: Reflector,
    ) -> None:
        """Create the tool set that an agent gets instead of the bank.

        Args:
            bank: Retain, entity, link, and invalidation operations.
            consolidation: Observation consolidation.
            models: Standing questions and their answers.
            recall: 4-way retrieval.
            reflector: Reflection in tiers.
        """

        self._bank = bank
        self._consolidation = consolidation
        self._models = models
        self._recall = recall
        self._reflector = reflector
        self.definitions = (
            ToolDefinition(
                name="register_entity",
                description="Declare a canonical entity and the surface forms it answers to.",
                parameters={
                    "name": "string",
                    "kind": "string",
                    "aliases": "list[string]",
                },
            ),
            ToolDefinition(
                name="retain",
                description=(
                    "Store one source payload and the facts extracted from it, resolving "
                    "entities and recording provenance."
                ),
                parameters={
                    "source": "string",
                    "content": "string",
                    "retained_at": "ISO 8601 timestamp",
                    "context": "string",
                    "facts": "list[fact object]",
                },
            ),
            ToolDefinition(
                name="link_memories",
                description="Connect two facts with a temporal, semantic, or causal link.",
                parameters={
                    "source_id": "memory id",
                    "target_id": "memory id",
                    "relation": "hindsight.temporal | hindsight.semantic | hindsight.causal",
                    "metadata": "object",
                },
            ),
            ToolDefinition(
                name="invalidate_memory",
                description="Soft-retire one fact so it stops being recalled but stays auditable.",
                parameters={"memory_id": "memory id"},
            ),
            ToolDefinition(
                name="consolidate",
                description="Fold the facts retained since the last run into versioned observations.",
                parameters={"at": "ISO 8601 timestamp"},
            ),
            ToolDefinition(
                name="create_mental_model",
                description="Register a standing question whose answer the bank keeps current.",
                parameters={"name": "string", "question": "string", "tags": "list[string]"},
            ),
            ToolDefinition(
                name="refresh_mental_model",
                description="Rebuild a standing answer, writing a version only if its scope changed.",
                parameters={"name": "string", "at": "ISO 8601 timestamp"},
            ),
            ToolDefinition(
                name="get_mental_model",
                description="Read the current standing answer and the versions before it.",
                parameters={"name": "string"},
            ),
            ToolDefinition(
                name="recall",
                description=(
                    "Rank facts and observations with the similarity, keyword, graph, and "
                    "temporal retrievers, fused and capped at a token budget."
                ),
                parameters={
                    "query": "string",
                    "entity": "optional string",
                    "types": "optional list[world | experience | observation]",
                    "tags": "optional list[string]",
                    "window_start": "optional ISO 8601 timestamp",
                    "window_end": "optional ISO 8601 timestamp",
                    "max_tokens": "integer",
                },
            ),
            ToolDefinition(
                name="reflect",
                description=(
                    "Answer from the highest layer that knows: mental model, then "
                    "observations, then raw facts, reporting the evidence used."
                ),
                parameters={
                    "question": "string",
                    "model_name": "optional string",
                    "query": "optional string",
                    "entity": "optional string",
                    "window_start": "optional ISO 8601 timestamp",
                    "window_end": "optional ISO 8601 timestamp",
                    "max_tokens": "integer",
                },
            ),
        )
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "register_entity": self._register_entity,
            "retain": self._retain,
            "link_memories": self._link_memories,
            "invalidate_memory": self._invalidate_memory,
            "consolidate": self._consolidate,
            "create_mental_model": self._create_mental_model,
            "refresh_mental_model": self._refresh_mental_model,
            "get_mental_model": self._get_mental_model,
            "recall": self._recall_tool,
            "reflect": self._reflect,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a named tool.

        Args:
            name: Tool name from `definitions`.
            arguments: JSON-compatible tool arguments.

        Returns:
            JSON-compatible tool result.

        Raises:
            ValueError: If the tool name is unknown.
            TypeError: If the arguments do not match the selected tool.
        """

        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown Hindsight tool {name!r}.")
        return handler(**arguments)

    def _register_entity(
        self,
        *,
        name: str,
        kind: str = "concept",
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """Implement the `register_entity` tool."""

        entity = self._bank.register_entity(name, kind=kind, aliases=aliases or ())
        return {
            "entity_id": entity.id,
            "name": entity.name,
            "kind": entity.kind,
            "aliases": list(entity.aliases),
        }

    def _retain(
        self,
        *,
        source: str,
        content: str,
        retained_at: str,
        context: str = "",
        facts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Implement the `retain` tool."""

        drafts = [_draft(item) for item in (facts or [])]
        result = self._bank.retain(
            source=source,
            content=content,
            retained_at=parse_timestamp(retained_at, field="retained_at"),
            context=context,
            facts=drafts,
        )
        return {
            "document_id": result.document.id,
            "memories": [_fact_result(fact) for fact in result.facts],
            "re_attested": [fact.id for fact in result.duplicates],
        }

    def _link_memories(
        self,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Implement the `link_memories` tool."""

        link = self._bank.connect(source_id, target_id, relation, metadata=metadata)
        return {
            "link_id": link.id,
            "source_id": link.source_id,
            "target_id": link.target_id,
            "relation": link.relation,
            "metadata": deepcopy(link.metadata),
        }

    def _invalidate_memory(self, *, memory_id: str) -> dict[str, Any]:
        """Implement the `invalidate_memory` tool."""

        return _fact_result(self._bank.invalidate(memory_id))

    def _consolidate(self, *, at: str) -> dict[str, Any]:
        """Implement the `consolidate` tool."""

        observations = self._consolidation.consolidate(at=parse_timestamp(at, field="at"))
        return {
            "at": at,
            "observations": [_observation_result(item) for item in observations],
            "cursor": self._consolidation.cursor(),
        }

    def _create_mental_model(
        self,
        *,
        name: str,
        question: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Implement the `create_mental_model` tool."""

        model = self._models.create(name=name, question=question, tags=tags or ())
        return _model_result(model)

    def _refresh_mental_model(self, *, name: str, at: str) -> dict[str, Any]:
        """Implement the `refresh_mental_model` tool."""

        before = self._models.current(name)
        model = self._models.refresh(name, at=parse_timestamp(at, field="at"))
        return {
            **_model_result(model),
            "rebuilt": before is None or before.revision != model.revision,
        }

    def _get_mental_model(self, *, name: str) -> dict[str, Any]:
        """Implement the `get_mental_model` tool."""

        model = self._models.current(name)
        if model is None:
            raise ValueError(f"Unknown mental model {name!r}.")
        return {
            **_model_result(model),
            "previous_versions": [
                {"revision": version.revision, "content": version.content}
                for version in self._models.history(name)
                if version.revision != model.revision
            ],
        }

    def _recall_tool(
        self,
        *,
        query: str,
        entity: str | None = None,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        max_tokens: int = 80,
    ) -> dict[str, Any]:
        """Implement the `recall` tool."""

        response = self._recall.recall(
            query,
            entity=entity,
            types=types or (),
            tags=tags or (),
            window=_window(window_start, window_end),
            max_tokens=max_tokens,
        )
        return _recall_result(response)

    def _reflect(
        self,
        *,
        question: str,
        model_name: str | None = None,
        query: str | None = None,
        entity: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        max_tokens: int = 80,
    ) -> dict[str, Any]:
        """Implement the `reflect` tool."""

        reflection = self._reflector.reflect(
            question,
            model_name=model_name,
            query=query,
            entity=entity,
            window=_window(window_start, window_end),
            max_tokens=max_tokens,
        )
        return _reflection_result(reflection)


class HindsightAgent:
    """Small agent that learns and answers only through named tools.

    - **Deterministic:** the decision policy is deterministic, so the example
      runs without an API key.
    - **`learn`:** sends each source payload through retention, links,
      corrections, consolidation, and a model refresh, in that order.
      Hindsight does the same automatically after every write.
    - **`answer`:** selects the read tools.
    - **No direct access:** the agent never gets the `Memory`, the handle, or
      the bank.
    """

    def __init__(self, *, name: str, tools: HindsightTools) -> None:
        """Create an agent with a Hindsight tool set.

        Args:
            name: Human-readable agent name.
            tools: The only interface for memory access that the agent gets.
        """

        self.name = name
        self.tools = tools.definitions
        self._toolbox = tools
        self._memory_ids: dict[str, str] = {}
        self.call_log: list[ToolCallRecord] = []

    def learn(
        self,
        documents: Sequence[DocumentCandidate],
        *,
        entities: Sequence[EntityCandidate] = (),
        models: Sequence[ModelCandidate] = (),
    ) -> None:
        """Retain payloads, then consolidate and refresh after each one.

        Args:
            documents: Source payloads in the order that the bank learned them.
            entities: Canonical entities that the agent declares before
                retention.
            models: Standing questions that the agent registers before
                retention, so every payload can refresh them.
        """

        for candidate in entities:
            self._call(
                "register_entity",
                {
                    "name": candidate.name,
                    "kind": candidate.kind,
                    "aliases": list(candidate.aliases),
                },
            )
        for model in models:
            self._call(
                "create_mental_model",
                {
                    "name": model.name,
                    "question": model.question,
                    "tags": list(model.tags),
                },
            )
        for document in documents:
            self._retain_document(document)
            self._call("consolidate", {"at": document.retained_at})
            for model in models:
                self._call(
                    "refresh_mental_model",
                    {"name": model.name, "at": document.retained_at},
                )

    def answer(
        self,
        question: str,
        *,
        model_name: str,
        query: str,
        entity: str,
        window_start: str,
        window_end: str,
        max_tokens: int = 45,
    ) -> str:
        """Answer a question with only the read tools.

        Args:
            question: User question to include in the report.
            model_name: Standing answer to read first.
            query: Retrieval text for recall and reflection.
            entity: Entity where graph retrieval starts.
            window_start: Inclusive start of the recall window. Only the
                `recall` call uses the window.
            window_end: Exclusive end of the recall window.
            max_tokens: Token budget for the `recall` and `reflect` calls.

        Returns:
            Readable answer that the agent builds from the tool results.
        """

        model = self._call("get_mental_model", {"name": model_name})
        recalled = self._call(
            "recall",
            {
                "query": query,
                "entity": entity,
                "window_start": window_start,
                "window_end": window_end,
                "max_tokens": max_tokens,
            },
        )
        reflection = self._call(
            "reflect",
            {
                "question": question,
                "model_name": model_name,
                "query": query,
                "entity": entity,
                "max_tokens": max_tokens,
            },
        )
        lines = [
            f"Question: {question}",
            f"Mental model {model['name']} r{model['revision']}: {model['content']}",
            *[
                f"- was r{version['revision']}: {version['content']}"
                for version in model["previous_versions"]
            ],
            (
                f"Recalled for {query!r} "
                f"({recalled['tokens_used']}/{recalled['max_tokens']} tokens, "
                f"{recalled['dropped']} dropped):"
            ),
            *[_hit_line(hit) for hit in recalled["hits"]],
            f"Reflection ({' -> '.join(reflection['trace'])}):",
            f"- {reflection['text']}",
            "Based on:",
            *[_evidence_line(item) for item in reflection["based_on"]],
        ]
        return "\n".join(lines)

    def memory_id(self, key: str) -> str:
        """Return the stored id of the fact that has this key.

        Args:
            key: Agent-local fact key.

        Returns:
            The stored memory block id.

        Raises:
            ValueError: If the agent never retained a fact with this key.
        """

        memory_id = self._memory_ids.get(key)
        if memory_id is None:
            raise ValueError(f"Unknown fact key {key!r}.")
        return memory_id

    def tool_usage(self) -> str:
        """Return a short summary of all tool calls.

        Returns:
            Tool names with their call counts.
        """

        counts = Counter(record.name for record in self.call_log)
        return ", ".join(f"{name} x{count}" for name, count in counts.items())

    def _retain_document(self, document: DocumentCandidate) -> None:
        """Retain one payload, then add its links and apply its corrections."""

        result = self._call(
            "retain",
            {
                "source": document.source,
                "content": document.content,
                "retained_at": document.retained_at,
                "context": document.context,
                "facts": [_fact_arguments(fact) for fact in document.facts],
            },
        )
        stored = list(result["memories"])
        for candidate, memory in zip(document.facts, stored, strict=True):
            self._memory_ids[candidate.key] = memory["id"]
        for link in document.links:
            self._call(
                "link_memories",
                {
                    "source_id": self.memory_id(link.source_key),
                    "target_id": self.memory_id(link.target_key),
                    "relation": link.relation,
                    "metadata": deepcopy(link.metadata),
                },
            )
        for key in document.corrections:
            self._call("invalidate_memory", {"memory_id": self.memory_id(key)})

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and add a copy of the call to `call_log`."""

        result = self._toolbox.call(name, arguments)
        self.call_log.append(
            ToolCallRecord(
                name=name,
                arguments=deepcopy(arguments),
                result=deepcopy(result),
            )
        )
        return result


def _draft(payload: dict[str, Any]) -> FactDraft:
    """Convert one JSON-compatible fact argument into a draft."""

    claim_key = payload.get("claim")
    belief = payload.get("belief")
    claim: Claim | None = None
    if claim_key is not None or belief is not None:
        if claim_key is None or belief is None:
            raise ValueError("A fact claim requires both 'claim' and 'belief'.")
        claim = Claim(key=claim_key, belief=belief, stance=payload.get("stance", "supports"))
    occurred_at = payload.get("occurred_at")
    occurred_until = payload.get("occurred_until")
    return FactDraft(
        text=payload["text"],
        fact_type=payload["fact_type"],
        context=payload.get("context", ""),
        entities=tuple(payload.get("entities", ())),
        occurred_at=None if occurred_at is None else parse_timestamp(occurred_at, field="occurred_at"),
        occurred_until=(
            None if occurred_until is None else parse_timestamp(occurred_until, field="occurred_until")
        ),
        tags=tuple(payload.get("tags", ())),
        claim=claim,
    )


def _fact_arguments(candidate: FactCandidate) -> dict[str, Any]:
    """Convert one agent fact candidate into JSON-compatible arguments."""

    return {
        "text": candidate.text,
        "fact_type": candidate.fact_type,
        "context": candidate.context,
        "entities": list(candidate.entities),
        "occurred_at": candidate.occurred_at,
        "occurred_until": candidate.occurred_until,
        "tags": list(candidate.tags),
        "claim": candidate.claim,
        "belief": candidate.belief,
        "stance": candidate.stance,
    }


def _fact_result(fact: MemoryFact) -> dict[str, Any]:
    """Convert a stored fact into a JSON-compatible tool result."""

    return {
        "id": fact.id,
        "text": fact.text,
        "fact_type": fact.fact_type,
        "state": fact.state,
        "entities": list(fact.entities),
        "tags": list(fact.tags),
        "learned_at": format_timestamp(fact.learned_at),
        "occurred_start": None if fact.occurred is None else format_timestamp(fact.occurred.valid_at),
        "occurred_end": (
            None
            if fact.occurred is None or fact.occurred.invalid_at is None
            else format_timestamp(fact.occurred.invalid_at)
        ),
        "claim": None if fact.claim is None else fact.claim.key,
        "document_id": fact.document_id,
    }


def _observation_result(observation: Observation) -> dict[str, Any]:
    """Convert an observation into a JSON-compatible tool result."""

    return {
        "id": observation.id,
        "claim": observation.claim,
        "text": observation.text,
        "revision": observation.revision,
        "stance": observation.stance,
        "proof_count": observation.proof_count,
        "sources": list(observation.sources),
        "tags": list(observation.tags),
        "stale": observation.stale,
        "consolidated_at": format_timestamp(observation.consolidated_at),
    }


def _model_result(model: MentalModel) -> dict[str, Any]:
    """Convert a mental model into a JSON-compatible tool result."""

    return {
        "id": model.id,
        "name": model.name,
        "question": model.question,
        "content": model.content,
        "revision": model.revision,
        "tags": list(model.tags),
        "sources": list(model.sources),
        "refreshed_at": None if model.refreshed_at is None else format_timestamp(model.refreshed_at),
    }


def _recall_result(response: RecallResponse) -> dict[str, Any]:
    """Convert a recall response into a JSON-compatible tool result."""

    return {
        "query": response.query,
        "hits": [
            {
                "id": hit.id,
                "text": hit.text,
                "kind": hit.kind,
                "score": hit.score,
                "retrievers": list(hit.retrievers),
                "tokens": hit.tokens,
            }
            for hit in response.hits
        ],
        "tokens_used": response.tokens_used,
        "max_tokens": response.max_tokens,
        "dropped": response.dropped,
    }


def _reflection_result(reflection: Reflection) -> dict[str, Any]:
    """Convert a reflection into a JSON-compatible tool result."""

    return {
        "question": reflection.question,
        "text": reflection.text,
        "trace": list(reflection.trace),
        "based_on": [_evidence_result(item) for item in reflection.based_on],
    }


def _evidence_result(evidence: Evidence) -> dict[str, Any]:
    """Convert one evidence item into a JSON-compatible tool result."""

    return {
        "kind": evidence.kind,
        "id": evidence.block_id,
        "text": evidence.text,
        "sources": list(evidence.sources),
    }


def _window(start: str | None, end: str | None) -> RecallWindow | None:
    """Build a recall window from 2 optional tool timestamps."""

    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError("A recall window needs both a start and an end.")
    return RecallWindow(
        start=parse_timestamp(start, field="window_start"),
        end=parse_timestamp(end, field="window_end"),
    )


def _hit_line(hit: dict[str, Any]) -> str:
    """Render one recall hit for the printed report."""

    retrievers = "+".join(hit["retrievers"])
    return f"- [{hit['kind']}] {hit['text']} ({retrievers}, {hit['score']:.4f})"


def _evidence_line(item: dict[str, Any]) -> str:
    """Render one evidence item for the printed report."""

    return f"- [{item['kind']}] {item['text']} (from {len(item['sources'])} source(s))"
