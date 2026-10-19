"""Value types, constants, and helpers for the Mem0 lamp.

- **One record type:** Mem0 stores a short memory in natural language. An id
  addresses the memory. The identifiers of its source session give its scope.
- **Decisions about that record:** the rest of Mem0 decides if a new fact adds
  a memory beside the record, merges into it, or removes it. A row in the
  history table records which of these occurred.

The 4 parts that the rest of the lamp shares:

- **Block types:** they keep memories, history events, and graph entities apart.
- **`Session`:** the type that holds `user_id`, `agent_id`, and `run_id`.
- **Records:** the read-only records that the stores return.
- **Text helpers:** the deterministic pipeline uses them to calculate scores.

This module does not write to Genie. Thus all other modules can import it
without an import cycle.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from genie import MemoryBlock

STORE_TYPE = "mem0.store"
ANCHOR_TYPE = "mem0.anchor"
SCOPE_TYPE = "mem0.scope"
MEMORY_TYPE = "mem0.memory"
RECORD_TYPE = "mem0.history_record"
EVENT_TYPE = "mem0.history_event"
ENTITY_TYPE = "mem0.entity"

RELATION_PREFIX = "mem0.rel."

TIERS = ("memories", "history", "entities")

MEMORY_NAMESPACE = ("memories",)
HISTORY_NAMESPACE = ("history",)

FACTUAL = "factual"
PROCEDURAL = "procedural_memory"
MEMORY_KINDS: frozenset[str] = frozenset({FACTUAL, PROCEDURAL})

MemoryEvent = Literal["ADD", "UPDATE", "DELETE", "NONE"]
MEMORY_EVENTS: tuple[str, ...] = ("ADD", "UPDATE", "DELETE", "NONE")

SCOPE_LEVELS: tuple[str, ...] = ("user", "agent", "run")

READ_LIMIT = 1_000

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def new_memory_id() -> str:
    """Create the id that Mem0 returns for a stored memory.

    Returns:
        A new UUID as a string, as upstream makes it.
    """

    return str(uuid4())


def content_hash(text: str) -> str:
    """Return the digest that Mem0 keeps with a memory to find a repeat.

    Upstream stores an MD5 digest of the memory text in the vector payload. The
    lamp uses the same digest. Thus the pipeline gets an exact-repeat check
    that adds no reads to the reads that it does already.

    Args:
        text: Memory text.

    Returns:
        Hex digest of the text.
    """

    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Return a timezone-aware timestamp in UTC.

    Args:
        value: Timestamp from application code.
        field: Field name for error messages.

    Returns:
        The same instant in UTC.

    Raises:
        TypeError: If `value` is not a `datetime`.
        ValueError: If `value` has no timezone.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Format a timestamp as a JSON-compatible ISO 8601 string in UTC.

    Args:
        value: Timezone-aware timestamp.

    Returns:
        An ISO 8601 string that ends in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name for error messages.

    Returns:
        The parsed instant in UTC.

    Raises:
        ValueError: If the text is not a timezone-aware ISO 8601 timestamp.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from error
    return normalize_timestamp(parsed, field=field)


def required_text(value: object, *, field: str) -> str:
    """Return required text without whitespace at the start and end.

    Args:
        value: Text to check.
        field: Field name for error messages.

    Returns:
        The text without whitespace at the start and end.

    Raises:
        ValueError: If the value is not a string, or is empty or only whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} cannot be empty.")
    return value.strip()


def optional_text(value: object, *, field: str) -> str | None:
    """Return optional text without whitespace at the start and end, or `None`.

    Args:
        value: Text to check, or `None`.
        field: Field name for error messages.

    Returns:
        The text without whitespace at the start and end, or `None` if the
        value is `None`.

    Raises:
        ValueError: If the value is not `None` and not a string, or if it is
            empty or only whitespace.
    """

    if value is None:
        return None
    return required_text(value, field=field)


def unique_strings(values: object, *, field: str) -> tuple[str, ...]:
    """Return the unique non-empty strings, in input order.

    Args:
        values: Iterable of strings to check.
        field: Field name for error messages.

    Returns:
        Unique strings without whitespace at the start and end.

    Raises:
        TypeError: If `values` is a string, a bytes value, or not iterable.
        ValueError: If an entry is not a string, or is empty or only whitespace.
    """

    if isinstance(values, str | bytes) or not isinstance(values, Iterable):
        raise TypeError(f"{field} must be a sequence of strings.")
    selected: list[str] = []
    for value in values:
        item = required_text(value, field=field)
        if item not in selected:
            selected.append(item)
    return tuple(selected)


def json_mapping(value: object, *, field: str) -> dict[str, Any]:
    """Return a separate copy of an optional JSON-compatible mapping.

    Args:
        value: Mapping to check, or `None`.
        field: Field name for error messages.

    Returns:
        A deep copy of the mapping, or an empty mapping if the value is `None`.

    Raises:
        TypeError: If the value is not `None` and not a mapping, or if a key is
            not a string.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field} keys must be strings.")
    return deepcopy(dict(cast(Mapping[str, Any], value)))


def tokenize(value: str) -> tuple[str, ...]:
    """Split text into lowercase alphanumeric tokens, in the order of first occurrence.

    The search terms keep their order. Thus a query causes the same sequence of
    Genie reads in each run.

    Args:
        value: Text to split.

    Returns:
        The distinct tokens in the text.
    """

    seen: list[str] = []
    for token in _TOKEN_PATTERN.findall(value.casefold()):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def overlap(left: Sequence[str], right: Sequence[str]) -> float:
    """Return how much 2 token sets share, from zero to one.

    - **Upstream:** Mem0 compares an extracted fact with stored memories through
      embedding similarity.
    - **Here:** Genie supplies no similarity measure, and the lamp runs with no
      model. Thus token overlap replaces embedding similarity.
    - **Value:** the result is the shared part of the 2 token sets. It is high
      for 2 wordings of the same fact, and low for 2 facts that are not related.

    Args:
        left: Tokens of the first text.
        right: Tokens of the second text.

    Returns:
        The number of shared tokens divided by the size of the union, rounded
        to 4 decimal places. The value is zero if one of the sets is empty.
    """

    first, second = set(left), set(right)
    if not first or not second:
        return 0.0
    return round(len(first & second) / len(first | second), 4)


def entity_key(name: str) -> str:
    """Return the stable lookup key for one graph entity name.

    The Mem0 graph phase resolves an extracted entity against the stored
    entities, so 2 spellings do not become 2 nodes. Here, case folding is the
    full resolution.

    Args:
        name: Entity name as extracted.

    Returns:
        The case-folded key that addresses the entity.
    """

    return required_text(name, field="entity name").casefold()


def relation_name(value: str) -> str:
    """Return the Genie link relation for one Mem0 relationship type.

    Args:
        value: Relationship type, for example `prefers`.

    Returns:
        The relation with the prefix of the lamp namespace.
    """

    return f"{RELATION_PREFIX}{required_text(value, field='relation')}"


def relation_label(relation: str) -> str:
    """Return the Mem0 relationship type of a Genie link relation.

    Args:
        relation: Link relation on the edge.

    Returns:
        The relationship type without the lamp prefix.
    """

    return relation.removeprefix(RELATION_PREFIX)


@dataclass(frozen=True, slots=True)
class Session:
    """The identifiers that Mem0 uses to scope a memory.

    - **Upstream:** these are flat payload fields. A search filters on the
      fields that the caller supplied.
    - **Here:** the lamp copies the supplied identifiers into an ownership path,
      in the fixed order below.
    - **Why:** a filter on the supplied ids and a read below the block that those
      ids name select the same memories. The ownership path also gives an answer
      that a filter cannot give: what happens to those memories when the user
      goes away.

    Attributes:
        user_id: Person that the memories belong to.
        agent_id: Agent that made the memories.
        run_id: One conversation or task in the scope of the user and agent.
        actor_id: Speaker of the turn that a memory came from. Upstream, this
            field makes a search narrower and does not set the place of the
            memory. Thus it stays metadata here.
    """

    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    actor_id: str | None = None

    def __post_init__(self) -> None:
        """Validate the identifiers.

        Raises:
            ValueError: If `user_id`, `agent_id`, and `run_id` are all `None`,
                or if an identifier is empty text.
        """

        for name in ("user_id", "agent_id", "run_id", "actor_id"):
            object.__setattr__(self, name, optional_text(getattr(self, name), field=name))
        if not any(getattr(self, f"{level}_id") for level in SCOPE_LEVELS):
            raise ValueError("A session needs at least one of user_id, agent_id, or run_id.")

    @property
    def path(self) -> tuple[str, ...]:
        """Return the ownership path that the supplied identifiers name."""

        return tuple(f"{level}:{getattr(self, f'{level}_id')}" for level in SCOPE_LEVELS if getattr(self, f"{level}_id"))

    @property
    def label(self) -> str:
        """Return the path as printable text, with a slash between each 2 segments."""

        return "/".join(self.path)

    @property
    def identifiers(self) -> dict[str, str | None]:
        """Return the identifiers as the portable fields that a block stores."""

        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "actor_id": self.actor_id,
        }

    def with_actor(self, actor_id: str | None) -> Session:
        """Return the same scope with a different speaker.

        Args:
            actor_id: The speaker, or `None` for no speaker.

        Returns:
            A session with the same path and the supplied actor.
        """

        return Session(
            user_id=self.user_id,
            agent_id=self.agent_id,
            run_id=self.run_id,
            actor_id=actor_id,
        )


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn that the caller gives to `add`.

    Attributes:
        role: Speaker role, for example `user` or `assistant`.
        content: Message text.
        name: Optional speaker name. Upstream uses it as the actor.
    """

    role: str
    content: str
    name: str | None = None

    def as_json(self) -> dict[str, str | None]:
        """Return the message in the form that Mem0 accepts."""

        return {"role": self.role, "content": self.content, "name": self.name}


@dataclass(frozen=True, slots=True)
class Fact:
    """One candidate memory from the extraction phase.

    Attributes:
        text: Memory text, as the lamp would store it.
        retracts: `True` if the fact says that a stored memory is no longer
            true. Upstream, a model reads the candidate together with the
            stored memory and makes this decision. Here, the extractor states
            it, because this decision is the one step that really needs a model.
        actor_id: Speaker that the fact came from, if a turn named a speaker.
        role: Role of that turn.
    """

    text: str
    retracts: bool = False
    actor_id: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        """Validate the candidate.

        Raises:
            TypeError: If `retracts` is not a boolean.
            ValueError: If the text is empty.
        """

        object.__setattr__(self, "text", required_text(self.text, field="fact text"))
        if not isinstance(self.retracts, bool):
            raise TypeError("retracts must be a boolean.")
        object.__setattr__(self, "actor_id", optional_text(self.actor_id, field="actor_id"))
        object.__setattr__(self, "role", optional_text(self.role, field="role"))


@dataclass(frozen=True, slots=True)
class Triplet:
    """One candidate relationship from the graph phase.

    The Mem0 graph tools call these fields `source`, `relationship`, and
    `destination`.

    Attributes:
        source: Entity at the start of the edge.
        relation: Relationship type.
        target: Entity at the end of the edge.
        retracts: `True` if the relationship is no longer true.
    """

    source: str
    relation: str
    target: str
    retracts: bool = False

    def __post_init__(self) -> None:
        """Validate the candidate.

        Raises:
            TypeError: If `retracts` is not a boolean.
            ValueError: If an endpoint or the relation is empty.
        """

        object.__setattr__(self, "source", required_text(self.source, field="source"))
        object.__setattr__(self, "relation", required_text(self.relation, field="relation"))
        object.__setattr__(self, "target", required_text(self.target, field="target"))
        if not isinstance(self.retracts, bool):
            raise TypeError("retracts must be a boolean.")


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """One memory, in the form that Mem0 returns it.

    Attributes:
        block: Genie block that holds the record.
        memory_id: Id that callers use to address the memory. It stays the same
            after updates.
        session: Scope that the memory came from.
        text: Memory text.
        hash: Digest of that text.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata from the write.
        created_at: Time of the first write of the memory.
        updated_at: Time of the last write of the memory.
    """

    block: MemoryBlock
    memory_id: str
    session: Session
    text: str
    hash: str
    kind: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def version(self) -> int:
        """Return how many times Genie wrote the block of this memory.

        A Mem0 update writes the record again in place and keeps its id. Genie
        counts the writes to one block. Thus this property shows the in-place
        change, and the lamp keeps no counter of its own.
        """

        return self.block.version

    @property
    def terms(self) -> tuple[str, ...]:
        """Return the tokens of the memory, for scores and comparisons."""

        return tokenize(self.text)

    def as_json(self) -> dict[str, Any]:
        """Return the record in the form that Mem0 returns."""

        return {
            "id": self.memory_id,
            "memory": self.text,
            "hash": self.hash,
            "memory_type": self.kind,
            "metadata": deepcopy(self.metadata),
            "created_at": format_timestamp(self.created_at),
            "updated_at": format_timestamp(self.updated_at),
            "version": self.version,
            **self.session.identifiers,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """The decision of the update phase for one candidate fact.

    Attributes:
        fact: Candidate that the decision is about.
        event: `ADD`, `UPDATE`, `DELETE`, or `NONE`.
        target_id: Memory that the event applies to. `None` for an `ADD`, and
            for a `NONE` that has no target memory.
        score: Similarity between the fact and that memory.
        reason: Plain-language explanation of the decision.
    """

    fact: Fact
    event: MemoryEvent
    target_id: str | None
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryChange:
    """One decision after the client applied it.

    Attributes:
        decision: `Decision` that caused the change.
        memory: Memory after the write, or the deleted memory for a `DELETE`.
            `None` for a `NONE`, which writes nothing.
        previous_text: Memory text before an `UPDATE` or `DELETE`.
    """

    decision: Decision
    memory: StoredMemory | None = None
    previous_text: str | None = None

    @property
    def event(self) -> MemoryEvent:
        """Return the applied event."""

        return self.decision.event

    def as_json(self) -> dict[str, Any]:
        """Return the change in the form that the Mem0 `add` call reports."""

        return {
            "event": self.event,
            "memory": self.decision.fact.text if self.memory is None else self.memory.text,
            "id": None if self.memory is None else self.memory.memory_id,
            "previous_memory": self.previous_text,
            "score": self.decision.score,
            "reason": self.decision.reason,
        }


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One row of the change history that Mem0 keeps for a memory.

    Attributes:
        block: Genie block that holds the recorded event.
        memory_id: Memory that the event belongs to.
        event: `ADD`, `UPDATE`, or `DELETE`. A `NONE` writes no history.
        old_memory: Text before the change. `None` for an `ADD`.
        new_memory: Text after the change. `None` for a `DELETE`.
        actor_id: Speaker that the change came from, if a turn named a speaker.
        role: Role of that turn.
        created_at: Time when the lamp applied the change.
        is_deleted: `True` if this event removed the memory.
        revision: Position in the history of that memory, if the read came from
            that history.
        sequence: Position in the store-wide journal, if the read came from the
            journal.
    """

    block: MemoryBlock
    memory_id: str
    event: MemoryEvent
    old_memory: str | None
    new_memory: str | None
    actor_id: str | None
    role: str | None
    created_at: datetime
    is_deleted: bool
    revision: int | None = None
    sequence: int | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the row in the form that the Mem0 `history` call returns."""

        return {
            "memory_id": self.memory_id,
            "event": self.event,
            "old_memory": self.old_memory,
            "new_memory": self.new_memory,
            "actor_id": self.actor_id,
            "role": self.role,
            "created_at": format_timestamp(self.created_at),
            "is_deleted": self.is_deleted,
            "revision": self.revision,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One memory that the retrieval of the lamp returns.

    Attributes:
        memory: The memory that matches.
        score: Fraction of the query terms that occur in the memory text.
        matched: Query terms that matched, in query order.
    """

    memory: StoredMemory
    score: float
    matched: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the hit in the form that the Mem0 `search` call returns."""

        return {**self.memory.as_json(), "score": self.score, "matched": list(self.matched)}


@dataclass(frozen=True, slots=True)
class GraphRelation:
    """One relationship in the graph half of the Mem0 memory.

    Attributes:
        source: Name of the entity at the start of the edge.
        relation: Relationship type.
        target: Name of the entity at the end of the edge.
        link_id: Id of the Genie link that holds the relationship.
        retracted_at: Time when the lamp marked the relationship as no longer
            true.
    """

    source: str
    relation: str
    target: str
    link_id: str
    retracted_at: datetime | None = None

    @property
    def retracted(self) -> bool:
        """Return `True` if the relationship has a retraction time."""

        return self.retracted_at is not None

    def as_json(self) -> dict[str, Any]:
        """Return the relationship in the form that the Mem0 graph search reports."""

        return {
            "source": self.source,
            "relationship": self.relation,
            "destination": self.target,
            "retracted": self.retracted,
        }


@dataclass(frozen=True, slots=True)
class GraphChange:
    """The result of the graph phase for one candidate relationship.

    Attributes:
        triplet: Candidate that the change is about.
        event: `ADD`, `DELETE`, or `NONE`.
        relation: Relationship after the change, if one exists.
        reason: Plain-language explanation of the decision.
    """

    triplet: Triplet
    event: MemoryEvent
    relation: GraphRelation | None
    reason: str

    def as_json(self) -> dict[str, Any]:
        """Return the change as a JSON-compatible result."""

        return {
            "event": self.event,
            "source": self.triplet.source,
            "relationship": self.triplet.relation,
            "destination": self.triplet.target,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AddResult:
    """All results of one `add` call.

    Mem0 returns the applied memory events. If the application configures a
    graph store, Mem0 also returns the relationship changes.

    Attributes:
        session: Scope of the call.
        changes: The result for each candidate fact, in candidate order.
        relations: The result for each candidate relationship.
    """

    session: Session
    changes: tuple[MemoryChange, ...] = ()
    relations: tuple[GraphChange, ...] = ()

    def applied(self, *events: str) -> tuple[MemoryChange, ...]:
        """Return the memory changes for the named events.

        Args:
            events: Event names to keep. If the caller gives no name, the
                method keeps all events.

        Returns:
            The changes that match, in candidate order.
        """

        kept = set(events) or set(MEMORY_EVENTS)
        return tuple(change for change in self.changes if change.event in kept)

    def as_json(self) -> dict[str, Any]:
        """Return the result in the form that the Mem0 `add` call returns."""

        return {
            "scope": self.session.label,
            "results": [change.as_json() for change in self.changes],
            "relations": [change.as_json() for change in self.relations],
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The 2 halves of one Mem0 search.

    Attributes:
        query: Search text.
        hits: Memories that match, in descending score order.
        relations: Relationships of the entities that the query names.
    """

    query: str
    hits: tuple[SearchHit, ...] = ()
    relations: tuple[GraphRelation, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the result in the form that the Mem0 `search` call returns."""

        return {
            "query": self.query,
            "results": [hit.as_json() for hit in self.hits],
            "relations": [relation.as_json() for relation in self.relations],
        }


def memory_value(
    session: Session,
    memory_id: str,
    text: str,
    *,
    kind: str,
    metadata: Mapping[str, Any],
    created_at: datetime,
    updated_at: datetime,
) -> dict[str, Any]:
    """Return the payload that one memory block stores.

    The field names follow the record that the Mem0 API returns. Thus the
    stored value and the tool result have the same form.

    Args:
        session: Scope that the memory came from.
        memory_id: Id that callers use to address the memory.
        text: Memory text.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata from the write.
        created_at: Time of the first write of the memory.
        updated_at: Time of the last write of the memory.

    Returns:
        The JSON-compatible payload.

    Raises:
        ValueError: If Mem0 does not define the kind.
    """

    if kind not in MEMORY_KINDS:
        raise ValueError(f"memory_type must be one of {sorted(MEMORY_KINDS)}.")
    return {
        "memory_id": required_text(memory_id, field="memory_id"),
        "memory": required_text(text, field="memory"),
        "hash": content_hash(required_text(text, field="memory")),
        "memory_type": kind,
        "metadata": json_mapping(metadata, field="metadata"),
        "created_at": format_timestamp(created_at),
        "updated_at": format_timestamp(updated_at),
        **session.identifiers,
    }


def memory_metadata(session: Session, memory_id: str, text: str, *, kind: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return the portable index metadata that the lamp stores with a memory.

    - **Upstream:** Mem0 keeps the session identifiers in the vector payload, so
      a search can filter on them.
    - **Here:** the lamp copies the same fields into block metadata. Thus Genie
      can make a search narrower before the lamp decodes each candidate.
    - **Application metadata:** the lamp copies it in too, because upstream a
      search filter can name any payload field. If a name occurs in both, the
      indexed field wins.

    Args:
        session: Scope that the memory came from.
        memory_id: Id that callers use to address the memory.
        text: Memory text. The lamp indexes its digest to find repeats.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata from the write.

    Returns:
        The JSON-compatible metadata mapping.
    """

    return {
        **json_mapping(metadata, field="metadata"),
        "memory_id": memory_id,
        "memory_type": kind,
        "hash": content_hash(text),
        "scope": session.label,
        **session.identifiers,
    }


def memory_from_block(block: MemoryBlock) -> StoredMemory:
    """Decode one stored memory.

    Args:
        block: Block that holds a memory payload.

    Returns:
        The decoded record.

    Raises:
        TypeError: If the block is not a stored Mem0 memory.
    """

    if block.type != MEMORY_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a Mem0 memory.")
    payload = cast(dict[str, Any], block.value)
    return StoredMemory(
        block=block,
        memory_id=required_text(payload.get("memory_id"), field="memory_id"),
        session=Session(
            user_id=cast(str | None, payload.get("user_id")),
            agent_id=cast(str | None, payload.get("agent_id")),
            run_id=cast(str | None, payload.get("run_id")),
            actor_id=cast(str | None, payload.get("actor_id")),
        ),
        text=required_text(payload.get("memory"), field="memory"),
        hash=required_text(payload.get("hash"), field="hash"),
        kind=required_text(payload.get("memory_type"), field="memory_type"),
        metadata=json_mapping(payload.get("metadata"), field="metadata"),
        created_at=parse_timestamp(required_text(payload.get("created_at"), field="created_at")),
        updated_at=parse_timestamp(required_text(payload.get("updated_at"), field="updated_at")),
    )


@dataclass(frozen=True, slots=True)
class Exchange:
    """One conversation that the caller gives to `add`, with the extraction results.

    Attributes:
        messages: Turns in the exchange.
        facts: Candidate memories, as the extraction phase would make them.
        relations: Candidate relationships, as the graph phase would make them.
    """

    messages: tuple[Message, ...] = ()
    facts: tuple[Fact, ...] = ()
    relations: tuple[Triplet, ...] = ()


def messages_from_json(payload: object) -> tuple[Message, ...]:
    """Build conversation turns from JSON-compatible tool arguments.

    Args:
        payload: Sequence of `{role, content, name}` mappings.

    Returns:
        The turns, in the supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If a turn has no role or no content.
    """

    return tuple(
        Message(
            role=required_text(entry.get("role"), field="role"),
            content=required_text(entry.get("content"), field="content"),
            name=optional_text(entry.get("name"), field="name"),
        )
        for entry in _entries(payload, field="messages")
    )


def facts_from_json(payload: object) -> tuple[Fact, ...]:
    """Build candidate memories from JSON-compatible tool arguments.

    Args:
        payload: Sequence of `{text, retracts, actor_id, role}` mappings.

    Returns:
        The candidates, in the supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If a candidate has no text.
    """

    return tuple(
        Fact(
            text=required_text(entry.get("text"), field="text"),
            retracts=bool(entry.get("retracts", False)),
            actor_id=optional_text(entry.get("actor_id"), field="actor_id"),
            role=optional_text(entry.get("role"), field="role"),
        )
        for entry in _entries(payload, field="facts")
    )


def triplets_from_json(payload: object) -> tuple[Triplet, ...]:
    """Build candidate relationships from JSON-compatible tool arguments.

    Args:
        payload: Sequence of `{source, relationship, destination, retracts}`
            mappings, with the field names that the Mem0 graph tools use.

    Returns:
        The candidates, in the supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If a mapping has no source, no destination, or no
            relationship.
    """

    return tuple(
        Triplet(
            source=required_text(entry.get("source"), field="source"),
            relation=required_text(entry.get("relationship"), field="relationship"),
            target=required_text(entry.get("destination"), field="destination"),
            retracts=bool(entry.get("retracts", False)),
        )
        for entry in _entries(payload, field="relations")
    )


def _entries(payload: object, *, field: str) -> tuple[Mapping[str, Any], ...]:
    """Return a validated sequence of JSON-compatible mappings.

    Args:
        payload: Sequence to check.
        field: Field name for error messages.

    Returns:
        The mappings, in the supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
    """

    if isinstance(payload, str | bytes) or not isinstance(payload, Iterable):
        raise TypeError(f"{field} must be a sequence of objects.")
    entries = tuple(payload)
    if any(not isinstance(entry, Mapping) for entry in entries):
        raise TypeError(f"{field} entries must be objects.")
    return cast(tuple[Mapping[str, Any], ...], entries)
