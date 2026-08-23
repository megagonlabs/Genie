"""Value types, constants, and time helpers for the Hindsight lamp.

Hindsight keeps four kinds of knowledge apart: source documents, the facts
extracted from them, the observations consolidated out of those facts, and the
mental models written from observations. This module names the block types and
relations that separation needs, the timestamp helpers every layer shares, and
the read-only records the bank hands back.

Nothing here calls Genie beyond reading blocks that were already stored, so
every other module in the lamp can depend on it without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from genie import MemoryBlock
from genie.structures import ValidityInterval

BANK_TYPE = "hindsight.bank"
ANCHOR_TYPE = "hindsight.anchor"
DOCUMENT_TYPE = "hindsight.document"
MEMORY_TYPE = "hindsight.memory"
ENTITY_TYPE = "hindsight.entity"
OBSERVATION_TYPE = "hindsight.observation"
OBSERVATION_REVISION_TYPE = "hindsight.observation_revision"
MENTAL_MODEL_TYPE = "hindsight.mental_model"
MENTAL_MODEL_REVISION_TYPE = "hindsight.mental_model_revision"

MENTIONS_RELATION = "hindsight.mentions"
DERIVED_FROM_RELATION = "hindsight.derived_from"
SEMANTIC_RELATION = "hindsight.semantic"
CAUSAL_RELATION = "hindsight.causal"
TEMPORAL_RELATION = "hindsight.temporal"
ASSOCIATIVE_RELATIONS: frozenset[str] = frozenset(
    {SEMANTIC_RELATION, CAUSAL_RELATION, TEMPORAL_RELATION}
)

ENTITY_NAMESPACE = ("entities",)
CLAIM_NAMESPACE = ("claims",)
MODEL_NAMESPACE = ("models",)

FactType = Literal["world", "experience"]
FACT_TYPES: frozenset[str] = frozenset({"world", "experience"})

RecallType = Literal["world", "experience", "observation"]
RECALL_TYPES: frozenset[str] = frozenset({"world", "experience", "observation"})

MemoryState = Literal["valid", "invalidated"]
MEMORY_STATES: frozenset[str] = frozenset({"valid", "invalidated"})

Stance = Literal["supports", "contradicts"]
STANCES: frozenset[str] = frozenset({"supports", "contradicts"})

SEARCH_LIMIT = 1_000

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


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


def required_text(value: str, *, field: str) -> str:
    """Return stripped required text.

    Args:
        value: Candidate text.
        field: Field name used in error messages.

    Returns:
        The text without surrounding whitespace.

    Raises:
        ValueError: If the text is empty.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} cannot be empty.")
    return value.strip()


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

    if isinstance(values, str) or not hasattr(values, "__iter__"):
        raise TypeError(f"{field} must be a sequence of strings.")
    selected: list[str] = []
    for value in values:
        item = required_text(value, field=field)
        if item not in selected:
            selected.append(item)
    return tuple(selected)


def normalize_name(value: str) -> str:
    """Return the lookup key for an entity name or alias.

    Names are compared case-insensitively with collapsed whitespace and no
    trailing periods, so `Alice C.` and `alice c` share one key.

    Args:
        value: Entity name or alias as written.

    Returns:
        The normalized lookup key.
    """

    collapsed = _WHITESPACE_PATTERN.sub(" ", value.strip().casefold())
    return " ".join(part.rstrip(".") for part in collapsed.split(" ") if part.rstrip("."))


def tokenize(value: str) -> frozenset[str]:
    """Split text into lowercase alphanumeric tokens.

    Args:
        value: Text to tokenize.

    Returns:
        The distinct tokens found in the text.
    """

    return frozenset(_TOKEN_PATTERN.findall(value.casefold()))


def estimate_tokens(value: str) -> int:
    """Estimate the token cost of text for the recall budget.

    Hindsight trims recall results to a `max_tokens` budget measured with a
    real tokenizer. Counting whitespace-separated words keeps the same
    behavior visible without adding a dependency.

    Args:
        value: Text whose cost is estimated.

    Returns:
        The estimated token count.
    """

    return len(value.split())


@dataclass(frozen=True, slots=True)
class Claim:
    """The standing belief one extracted fact is evidence for.

    Upstream Hindsight decides which facts belong to which belief with a
    language model. The lamp asks the extractor for that decision instead, so
    consolidation stays deterministic.

    Attributes:
        key: Stable identifier for the belief this fact speaks to.
        belief: Sentence the fact supports or contradicts.
        stance: Whether the fact backs the belief or overturns it.
    """

    key: str
    belief: str
    stance: Stance = "supports"

    def __post_init__(self) -> None:
        """Validate the claim key, belief sentence, and stance."""

        object.__setattr__(self, "key", required_text(self.key, field="claim key"))
        object.__setattr__(self, "belief", required_text(self.belief, field="claim belief"))
        if self.stance not in STANCES:
            raise ValueError("claim stance must be 'supports' or 'contradicts'.")

    def as_value(self) -> dict[str, str]:
        """Return the JSON-compatible stored form of this claim."""

        return {"key": self.key, "belief": self.belief, "stance": self.stance}


@dataclass(frozen=True, slots=True)
class FactDraft:
    """One extracted fact waiting to be retained.

    Attributes:
        text: Narrative sentence stored as the memory.
        fact_type: `world` for facts about others, `experience` for the
            bank's own first-person history.
        context: Short description of the situation the fact came from.
        entities: Entity names the fact mentions, as written.
        occurred_at: Optional start of the period the fact describes.
        occurred_until: Optional exclusive end of that period.
        tags: Visibility tags used for scoping and recall filters.
        claim: Optional belief this fact is evidence for.
    """

    text: str
    fact_type: FactType
    context: str = ""
    entities: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    occurred_until: datetime | None = None
    tags: tuple[str, ...] = ()
    claim: Claim | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """A retained source payload, before extraction.

    Attributes:
        block: Stored document block.
        source: Where the payload came from.
        content: Raw payload text.
        context: Caller-supplied framing for the payload.
        retained_at: When the bank learned this payload.
    """

    block: MemoryBlock
    source: str
    content: str
    context: str
    retained_at: datetime

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class MemoryFact:
    """One atomic fact extracted from a document.

    Attributes:
        block: Stored fact block.
        text: Narrative sentence.
        fact_type: `world` or `experience`.
        state: `valid` or soft-retired `invalidated`.
        context: Situation the fact came from.
        entities: Resolved canonical entity names.
        occurred: Optional period the fact describes, or `None` for an
            ongoing fact such as a preference.
        learned_at: When the bank learned the fact.
        tags: Visibility tags.
        claim: Belief this fact is evidence for, when it has one.
        document_id: Source document block id.
    """

    block: MemoryBlock
    text: str
    fact_type: FactType
    state: MemoryState
    context: str
    entities: tuple[str, ...]
    occurred: ValidityInterval | None
    learned_at: datetime
    tags: tuple[str, ...]
    claim: Claim | None
    document_id: str

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class Entity:
    """A canonical person, place, product, or concept.

    Attributes:
        block: Stored entity block.
        name: Canonical display name.
        kind: Application-defined entity kind.
        aliases: Every surface form bound to this entity so far.
    """

    block: MemoryBlock
    name: str
    kind: str
    aliases: tuple[str, ...]

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class Observation:
    """A consolidated belief and the evidence standing behind it.

    Attributes:
        identity: Stable block every revision of this belief hangs from.
        block: Block holding the current revision's text.
        claim: Belief key shared by every revision.
        text: Current wording of the belief.
        revision: Positive revision number of `block`.
        stance: Whether this revision recorded support or a reversal.
        tags: Visibility tags inherited from the supporting facts.
        consolidated_at: When this revision was written.
        proof_count: Number of facts linked to this revision as evidence.
        sources: Ids of those supporting facts.
        stale: Whether facts touching this belief are still unconsolidated.
    """

    identity: MemoryBlock
    block: MemoryBlock
    claim: str
    text: str
    revision: int
    stance: Stance
    tags: tuple[str, ...]
    consolidated_at: datetime
    proof_count: int
    sources: tuple[str, ...]
    stale: bool

    @property
    def id(self) -> str:
        """Return the block id of this revision."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class MentalModel:
    """A standing question and the answer currently on file for it.

    Attributes:
        identity: Stable block every revision of this answer hangs from.
        block: Block holding the current revision's content, or `None` before
            the first refresh.
        name: Stable model name.
        question: Standing question the model answers.
        content: Current answer text, empty before the first refresh.
        revision: Positive revision number, or zero before the first refresh.
        tags: Scope tags restricting which memories feed the model.
        refreshed_at: When this revision was written, if there is one.
        sources: Ids of the observations and facts this revision was built
            from.
    """

    identity: MemoryBlock
    block: MemoryBlock | None
    name: str
    question: str
    content: str
    revision: int
    tags: tuple[str, ...]
    refreshed_at: datetime | None
    sources: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Return the block id of the current revision, or the identity."""

        return self.block.id if self.block is not None else self.identity.id


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One ranked recall result.

    Attributes:
        block: Fact or observation revision block that matched.
        text: Text the retrievers scored.
        kind: `world`, `experience`, or `observation`.
        score: Fused reciprocal-rank score, higher first.
        retrievers: Names of the retrievers that found this candidate.
        tokens: Estimated token cost charged to the recall budget.
    """

    block: MemoryBlock
    text: str
    kind: RecallType
    score: float
    retrievers: tuple[str, ...]
    tokens: int

    @property
    def id(self) -> str:
        """Return the underlying Genie block id."""

        return self.block.id


@dataclass(frozen=True, slots=True)
class Evidence:
    """One item the reflection leaned on, with where it came from.

    Attributes:
        kind: `mental_model`, `observation`, `world`, or `experience`.
        block_id: Block the evidence was read from.
        text: Text the answer drew on, or the name and revision of the
            standing answer it came from.
        sources: Ids of the blocks this item was derived from, walked back
            through the lineage relation.
    """

    kind: str
    block_id: str
    text: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Reflection:
    """The answer a reflection produced and how it got there.

    Attributes:
        question: Question that was reflected on.
        text: Composed answer.
        based_on: Evidence used, highest tier first.
        trace: Retrieval tiers consulted, in order.
    """

    question: str
    text: str
    based_on: tuple[Evidence, ...] = ()
    trace: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetainResult:
    """What one `retain` call stored.

    Attributes:
        document: The stored source payload.
        facts: One fact per draft, in draft order. A draft whose text was
            already on file resolves to the existing fact.
        duplicates: The subset of `facts` that this payload re-attested; each
            gained the new document as extra provenance instead of a copy.
    """

    document: Document
    facts: tuple[MemoryFact, ...] = ()
    duplicates: tuple[MemoryFact, ...] = field(default_factory=tuple)


def document_from_block(block: MemoryBlock) -> Document:
    """Decode a stored document block.

    Args:
        block: Block previously written by the bank.

    Returns:
        The decoded document.

    Raises:
        TypeError: If the block is not a document.
    """

    payload = _require_payload(block, DOCUMENT_TYPE)
    return Document(
        block=block,
        source=_string(payload, "source"),
        content=_string(payload, "content"),
        context=_string(payload, "context"),
        retained_at=parse_timestamp(_string(payload, "retained_at"), field="retained_at"),
    )


def fact_from_block(block: MemoryBlock) -> MemoryFact:
    """Decode a stored fact block.

    Args:
        block: Block previously written by the bank.

    Returns:
        The decoded fact.

    Raises:
        TypeError: If the block is not a fact.
        ValueError: If a stored taxonomy value is unknown.
    """

    payload = _require_payload(block, MEMORY_TYPE)
    fact_type = _fact_type(_string(payload, "fact_type"))
    state = _state(_string(payload, "state"))
    occurred_start = payload.get("occurred_start")
    occurred_end = payload.get("occurred_end")
    occurred = (
        ValidityInterval(
            valid_at=parse_timestamp(occurred_start, field="occurred_start"),
            invalid_at=(
                parse_timestamp(occurred_end, field="occurred_end")
                if isinstance(occurred_end, str)
                else None
            ),
        )
        if isinstance(occurred_start, str)
        else None
    )
    stored_claim = payload.get("claim")
    claim = (
        Claim(
            key=_string(stored_claim, "key"),
            belief=_string(stored_claim, "belief"),
            stance=_stance(_string(stored_claim, "stance")),
        )
        if isinstance(stored_claim, dict)
        else None
    )
    return MemoryFact(
        block=block,
        text=_string(payload, "text"),
        fact_type=fact_type,
        state=state,
        context=_string(payload, "context"),
        entities=_strings(payload, "entities"),
        occurred=occurred,
        learned_at=parse_timestamp(_string(payload, "learned_at"), field="learned_at"),
        tags=_strings(payload, "tags"),
        claim=claim,
        document_id=_string(payload, "document_id"),
    )


def entity_from_block(block: MemoryBlock) -> Entity:
    """Decode a stored entity block.

    Args:
        block: Block previously written by the bank.

    Returns:
        The decoded entity.

    Raises:
        TypeError: If the block is not an entity.
    """

    payload = _require_payload(block, ENTITY_TYPE)
    return Entity(
        block=block,
        name=_string(payload, "name"),
        kind=_string(payload, "kind"),
        aliases=_strings(payload, "aliases"),
    )


def _fact_type(value: str) -> FactType:
    """Validate and narrow a stored fact type."""

    if value not in FACT_TYPES:
        raise ValueError("fact_type must be 'world' or 'experience'.")
    return cast(FactType, value)


def _state(value: str) -> MemoryState:
    """Validate and narrow a stored curation state."""

    if value not in MEMORY_STATES:
        raise ValueError("state must be 'valid' or 'invalidated'.")
    return cast(MemoryState, value)


def _stance(value: str) -> Stance:
    """Validate and narrow a stored stance."""

    if value not in STANCES:
        raise ValueError("stance must be 'supports' or 'contradicts'.")
    return cast(Stance, value)


def _require_payload(block: MemoryBlock, expected: str) -> dict[str, Any]:
    """Return a block's dictionary value after checking its type."""

    if block.type != expected:
        raise TypeError(f"Expected block type {expected!r}, got {block.type!r}.")
    if not isinstance(block.value, dict):
        raise TypeError(f"{expected} value must be a dictionary.")
    return block.value


def _string(payload: dict[str, Any], name: str) -> str:
    """Read one required string field from a stored payload."""

    value = payload.get(name)
    if not isinstance(value, str):
        raise TypeError(f"Stored field {name!r} must be a string.")
    return value


def _strings(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    """Read one list-of-strings field from a stored payload."""

    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"Stored field {name!r} must be a list of strings.")
    return tuple(value)
