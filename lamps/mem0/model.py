"""Value types, constants, and helpers for the Mem0 lamp.

Mem0 stores one thing: a short natural-language memory, addressed by an id and
scoped by the identifiers of the session it came from. Everything else in the
system is a decision about that record — whether a new fact should be added
beside it, folded into it, or allowed to remove it — and a row in the history
table saying which of those happened.

This module holds the four things the rest of the lamp shares: the block types
that keep memories, history events, and graph entities apart, the session type
that carries `user_id`, `agent_id`, and `run_id`, the read-only records the
stores hand back, and the text helpers the deterministic pipeline scores with.
Nothing here writes to Genie, so every other module can depend on it without a
cycle.
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
    """Create the id Mem0 hands back for a stored memory.

    Returns:
        A fresh UUID in string form, as upstream generates.
    """

    return str(uuid4())


def content_hash(text: str) -> str:
    """Return the digest Mem0 keeps beside a memory to detect a repeat.

    Upstream stores an MD5 of the memory text in the vector payload. Reusing
    that here gives the pipeline an exact-repeat check that costs no reads
    beyond the ones it already makes.

    Args:
        text: Memory text.

    Returns:
        Hex digest of the text.
    """

    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Return a timezone-aware timestamp normalized to UTC.

    Args:
        value: Timestamp supplied by application code.
        field: Field name used in error messages.

    Returns:
        The same instant expressed in UTC.

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
        An ISO 8601 string ending in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name used in error messages.

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
    """Return stripped required text.

    Args:
        value: Candidate text.
        field: Field name used in error messages.

    Returns:
        The text without surrounding whitespace.

    Raises:
        ValueError: If the value is not a non-empty string.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} cannot be empty.")
    return value.strip()


def optional_text(value: object, *, field: str) -> str | None:
    """Return stripped optional text, or `None` when it was not supplied.

    Args:
        value: Candidate text or `None`.
        field: Field name used in error messages.

    Returns:
        The stripped text, or `None`.

    Raises:
        ValueError: If a value was supplied but is not a non-empty string.
    """

    if value is None:
        return None
    return required_text(value, field=field)


def unique_strings(values: object, *, field: str) -> tuple[str, ...]:
    """Return unique non-empty strings in input order.

    Args:
        values: Iterable of candidate strings.
        field: Field name used in error messages.

    Returns:
        Unique stripped strings.

    Raises:
        TypeError: If `values` is not a sequence of strings.
        ValueError: If any entry is empty.
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
    """Return an isolated copy of an optional JSON-compatible mapping.

    Args:
        value: Candidate mapping or `None`.
        field: Field name used in error messages.

    Returns:
        A deep copy of the mapping, empty when nothing was supplied.

    Raises:
        TypeError: If a value was supplied but is not a mapping with string
            keys.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field} keys must be strings.")
    return deepcopy(dict(cast(Mapping[str, Any], value)))


def tokenize(value: str) -> tuple[str, ...]:
    """Split text into lowercase alphanumeric tokens in first-seen order.

    Search terms keep their order so that a query produces the same sequence of
    Genie reads on every run.

    Args:
        value: Text to tokenize.

    Returns:
        The distinct tokens found in the text.
    """

    seen: list[str] = []
    for token in _TOKEN_PATTERN.findall(value.casefold()):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def overlap(left: Sequence[str], right: Sequence[str]) -> float:
    """Return how much two token sets share, between zero and one.

    Mem0 compares an extracted fact against stored memories with embedding
    similarity. Genie supplies no similarity measure and the lamp runs with no
    model, so token overlap stands in for it: the shared share of the two token
    sets, which is high for two wordings of the same fact and low for two
    unrelated ones.

    Args:
        left: Tokens of the first text.
        right: Tokens of the second text.

    Returns:
        Shared tokens divided by the size of the union, rounded to four places.
    """

    first, second = set(left), set(right)
    if not first or not second:
        return 0.0
    return round(len(first & second) / len(first | second), 4)


def entity_key(name: str) -> str:
    """Return the stable lookup key for one graph entity name.

    Mem0's graph phase resolves an extracted entity against the ones already
    stored so that two spellings do not become two nodes. Case folding is the
    whole of that resolution here.

    Args:
        name: Entity name as extracted.

    Returns:
        The folded key the entity is addressed by.
    """

    return required_text(name, field="entity name").casefold()


def relation_name(value: str) -> str:
    """Return the Genie link relation used for one Mem0 relationship type.

    Args:
        value: Relationship type such as `prefers`.

    Returns:
        The relation prefixed into the lamp's own namespace.
    """

    return f"{RELATION_PREFIX}{required_text(value, field='relation')}"


def relation_label(relation: str) -> str:
    """Return the Mem0 relationship type behind a Genie link relation.

    Args:
        relation: Link relation stored on the edge.

    Returns:
        The relationship type without the lamp's prefix.
    """

    return relation.removeprefix(RELATION_PREFIX)


@dataclass(frozen=True, slots=True)
class Session:
    """The identifiers Mem0 scopes a memory by.

    Upstream these are flat payload fields and a search filters on whichever of
    them the caller supplied. This lamp mirrors the supplied ones as an
    ownership path instead, in the fixed order below, because "filter on the ids
    I gave you" and "look below the block those ids name" select exactly the
    same memories — and the second one also answers what a filter cannot, which
    is what happens to those memories when the user goes away.

    Attributes:
        user_id: Person the memories belong to.
        agent_id: Agent that formed them.
        run_id: One conversation or task within the pair above.
        actor_id: Who spoke the turn a memory came from. Upstream this narrows
            a search rather than placing the memory, so it stays metadata here.
    """

    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    actor_id: str | None = None

    def __post_init__(self) -> None:
        """Validate the identifiers.

        Raises:
            ValueError: If every scoping identifier is missing, or one was
                supplied as empty text.
        """

        for name in ("user_id", "agent_id", "run_id", "actor_id"):
            object.__setattr__(self, name, optional_text(getattr(self, name), field=name))
        if not any(getattr(self, f"{level}_id") for level in SCOPE_LEVELS):
            raise ValueError("A session needs at least one of user_id, agent_id, or run_id.")

    @property
    def path(self) -> tuple[str, ...]:
        """Return the ownership path the supplied identifiers name."""

        return tuple(f"{level}:{getattr(self, f'{level}_id')}" for level in SCOPE_LEVELS if getattr(self, f"{level}_id"))

    @property
    def label(self) -> str:
        """Return the printable slash-separated form of the path."""

        return "/".join(self.path)

    @property
    def identifiers(self) -> dict[str, str | None]:
        """Return the identifiers as the portable fields stored on a block."""

        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "actor_id": self.actor_id,
        }

    def with_actor(self, actor_id: str | None) -> Session:
        """Return the same scope with a different speaker recorded.

        Args:
            actor_id: Who spoke, or `None` to record nobody.

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
    """One conversation turn handed to `add`.

    Attributes:
        role: Speaker role, such as `user` or `assistant`.
        content: Message text.
        name: Optional speaker name, which upstream carries as the actor.
    """

    role: str
    content: str
    name: str | None = None

    def as_json(self) -> dict[str, str | None]:
        """Return the message in the shape Mem0 accepts."""

        return {"role": self.role, "content": self.content, "name": self.name}


@dataclass(frozen=True, slots=True)
class Fact:
    """One candidate memory the extraction phase produced.

    Attributes:
        text: Memory as it would be stored.
        retracts: Whether the fact says something on file is no longer true.
            Upstream a model reads the candidate beside the stored memory and
            judges that; here the extractor states it, because judging it is
            the one step that genuinely needs a model.
        actor_id: Who the fact came from, when a turn named a speaker.
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
    """One candidate relationship the graph phase produced.

    Mem0's graph tools name these `source`, `relationship`, and `destination`.

    Attributes:
        source: Entity the edge starts from.
        relation: Relationship type.
        target: Entity the edge points to.
        retracts: Whether the relationship no longer holds.
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
    """One memory as Mem0 returns it.

    Attributes:
        block: Genie block holding the record.
        memory_id: Id callers address the memory by, stable across updates.
        session: Scope the memory was formed in.
        text: Memory text.
        hash: Digest of that text.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata supplied with the write.
        created_at: When the memory was first stored.
        updated_at: When it was last written.
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
        """Return how many times the underlying block has been written.

        A Mem0 update rewrites the record in place and keeps its id. Genie
        counts writes to one block, so the in-place patch is visible here
        without the lamp maintaining a counter.
        """

        return self.block.version

    @property
    def terms(self) -> tuple[str, ...]:
        """Return the memory's tokens, used for scoring and comparison."""

        return tokenize(self.text)

    def as_json(self) -> dict[str, Any]:
        """Return the record in the shape Mem0 hands back."""

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
    """What the update phase decided to do with one candidate fact.

    Attributes:
        fact: Candidate the decision is about.
        event: `ADD`, `UPDATE`, `DELETE`, or `NONE`.
        target_id: Memory the event applies to, absent for an `ADD`.
        score: Similarity between the fact and that memory.
        reason: Plain-language explanation of the choice.
    """

    fact: Fact
    event: MemoryEvent
    target_id: str | None
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryChange:
    """One decision after it was carried out.

    Attributes:
        decision: `Decision` that produced the change.
        memory: Memory after the write, or the deleted memory for a `DELETE`.
            Absent for a `NONE`, which writes nothing.
        previous_text: Memory text before an `UPDATE` or `DELETE`.
    """

    decision: Decision
    memory: StoredMemory | None = None
    previous_text: str | None = None

    @property
    def event(self) -> MemoryEvent:
        """Return the event that was applied."""

        return self.decision.event

    def as_json(self) -> dict[str, Any]:
        """Return the change in the shape Mem0's `add` reports."""

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
    """One row of the change history Mem0 keeps for a memory.

    Attributes:
        block: Genie block holding the recorded event.
        memory_id: Memory the event belongs to.
        event: `ADD`, `UPDATE`, or `DELETE`. A `NONE` writes no history.
        old_memory: Text before the change, absent for an `ADD`.
        new_memory: Text after it, absent for a `DELETE`.
        actor_id: Who the change came from, when a turn named a speaker.
        role: Role of that turn.
        created_at: When the change was applied.
        is_deleted: Whether this event removed the memory.
        revision: Position in that memory's own history, when read from it.
        sequence: Position in the store-wide journal, when read from that.
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
        """Return the row in the shape Mem0's `history` hands back."""

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
    """One memory returned by the lamp's retrieval.

    Attributes:
        memory: Matching memory.
        score: Share of query terms found in the memory.
        matched: Query terms that matched, in query order.
    """

    memory: StoredMemory
    score: float
    matched: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the hit in the shape Mem0's `search` hands back."""

        return {**self.memory.as_json(), "score": self.score, "matched": list(self.matched)}


@dataclass(frozen=True, slots=True)
class GraphRelation:
    """One relationship in the graph half of Mem0's memory.

    Attributes:
        source: Entity name the edge starts from.
        relation: Relationship type.
        target: Entity name the edge points to.
        link_id: Id of the Genie link carrying it.
        retracted_at: When the relationship was marked no longer true.
    """

    source: str
    relation: str
    target: str
    link_id: str
    retracted_at: datetime | None = None

    @property
    def retracted(self) -> bool:
        """Return whether the relationship has been retracted."""

        return self.retracted_at is not None

    def as_json(self) -> dict[str, Any]:
        """Return the relationship in the shape Mem0's graph search reports."""

        return {
            "source": self.source,
            "relationship": self.relation,
            "destination": self.target,
            "retracted": self.retracted,
        }


@dataclass(frozen=True, slots=True)
class GraphChange:
    """What the graph phase did with one candidate relationship.

    Attributes:
        triplet: Candidate the change is about.
        event: `ADD`, `DELETE`, or `NONE`.
        relation: Relationship after the change, when there is one.
        reason: Plain-language explanation of the choice.
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
    """Everything one `add` call produced.

    Mem0 returns the applied memory events and, when a graph store is
    configured, the relationship changes beside them.

    Attributes:
        session: Scope the call was made in.
        changes: What happened to every candidate fact, in candidate order.
        relations: What happened to every candidate relationship.
    """

    session: Session
    changes: tuple[MemoryChange, ...] = ()
    relations: tuple[GraphChange, ...] = ()

    def applied(self, *events: str) -> tuple[MemoryChange, ...]:
        """Return the memory changes matching the named events.

        Args:
            events: Event names to keep. All events are kept when none is
                given.

        Returns:
            Matching changes in candidate order.
        """

        kept = set(events) or set(MEMORY_EVENTS)
        return tuple(change for change in self.changes if change.event in kept)

    def as_json(self) -> dict[str, Any]:
        """Return the result in the shape Mem0's `add` hands back."""

        return {
            "scope": self.session.label,
            "results": [change.as_json() for change in self.changes],
            "relations": [change.as_json() for change in self.relations],
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Both halves of one Mem0 search.

    Attributes:
        query: Text that was searched for.
        hits: Matching memories in descending score order.
        relations: Relationships attached to the entities the query named.
    """

    query: str
    hits: tuple[SearchHit, ...] = ()
    relations: tuple[GraphRelation, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the result in the shape Mem0's `search` hands back."""

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
    """Return the payload one memory block stores.

    The field names follow the record Mem0's API hands back, so the stored
    value and the tool result are the same shape.

    Args:
        session: Scope the memory was formed in.
        memory_id: Id callers address the memory by.
        text: Memory text.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata supplied with the write.
        created_at: When the memory was first stored.
        updated_at: When it was last written.

    Returns:
        The JSON-compatible payload.

    Raises:
        ValueError: If the kind is not one Mem0 defines.
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
    """Return the portable index metadata stored beside a memory.

    Mem0 keeps the session identifiers in the vector payload so a search can
    filter on them. The same fields are duplicated into block metadata here so
    Genie can narrow a search without the lamp decoding every candidate first.
    Application metadata is copied in as well, because upstream a search filter
    may name any payload field; the indexed fields win a name collision.

    Args:
        session: Scope the memory was formed in.
        memory_id: Id callers address the memory by.
        text: Memory text, whose digest is indexed for repeat detection.
        kind: `factual` or `procedural_memory`.
        metadata: Application metadata supplied with the write.

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
        block: Block holding a memory payload.

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
    """One conversation handed to `add`, with what was extracted from it.

    Attributes:
        messages: Turns in the exchange.
        facts: Candidate memories the extraction phase would have produced.
        relations: Candidate relationships the graph phase would have produced.
    """

    messages: tuple[Message, ...] = ()
    facts: tuple[Fact, ...] = ()
    relations: tuple[Triplet, ...] = ()


def messages_from_json(payload: object) -> tuple[Message, ...]:
    """Build conversation turns from JSON-compatible tool arguments.

    Args:
        payload: Sequence of `{role, content, name}` mappings.

    Returns:
        The turns in supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If a turn is missing its role or content.
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
        The candidates in supplied order.

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
            mappings, using the field names Mem0's graph tools use.

    Returns:
        The candidates in supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If an endpoint or the relationship is missing.
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
        payload: Candidate sequence.
        field: Field name used in error messages.

    Returns:
        The mappings in supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
    """

    if isinstance(payload, str | bytes) or not isinstance(payload, Iterable):
        raise TypeError(f"{field} must be a sequence of objects.")
    entries = tuple(payload)
    if any(not isinstance(entry, Mapping) for entry in entries):
        raise TypeError(f"{field} entries must be objects.")
    return cast(tuple[Mapping[str, Any], ...], entries)
