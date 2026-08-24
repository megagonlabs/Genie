"""Value types, constants, and the arithmetic the MemoryOS lamp shares.

MemoryOS borrows its shape from an operating system. A dialogue page is the unit
that moves, short-term memory is the fast tier that holds only the newest few,
mid-term memory is the paged tier where pages are grouped into topic segments,
and long-term memory is the slow tier that keeps what turned out to matter. What
decides whether something is promoted is not age but *heat*: a segment's score
rises when a retrieval visits it, when it gains pages, and falls as it is left
alone.

Three things in this module are the reason the rest of the lamp can be small.
The block types keep the four tiers' contents apart. The records are what the
tiers hand back, each with a JSON form so the same values can cross a tool
boundary. And `segment_heat` and `time_decay` are MemoryOS's own formulas,
written once here so both the updater and the retriever read heat the same way.

Nothing in this module touches Genie, so every other module can depend on it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from genie import MemoryBlock
from genie.structures import MemoryAddress

SYSTEM_TYPE = "memoryos.system"
TIER_TYPE = "memoryos.tier"
QA_TYPE = "memoryos.qa_pair"
SEGMENT_TYPE = "memoryos.segment"
PAGE_TYPE = "memoryos.page"
PROFILE_TYPE = "memoryos.profile"
REVISION_TYPE = "memoryos.profile_revision"
KNOWLEDGE_TYPE = "memoryos.knowledge"

NEXT_PAGE = "memoryos.next_page"
EXTRACTED_FROM = "memoryos.extracted_from"

TIERS: tuple[str, ...] = ("short_term", "mid_term", "user_long_term", "assistant_long_term")

PAGE_NAMESPACE: tuple[str, ...] = ("pages",)
SEGMENT_NAMESPACE: tuple[str, ...] = ("segments",)
PROFILE_ADDRESS = MemoryAddress(("long_term",), "user_profile")

USER = "user"
ASSISTANT = "assistant"
KNOWLEDGE_OWNERS: tuple[str, ...] = (USER, ASSISTANT)
KnowledgeOwner = Literal["user", "assistant"]

HEAT_ALPHA = 1.0
"""Weight MemoryOS gives to how often a segment has been visited."""

HEAT_BETA = 1.0
"""Weight MemoryOS gives to how much unanalyzed dialogue a segment holds."""

HEAT_GAMMA = 1.0
"""Weight MemoryOS gives to how recently a segment was visited."""

RECENCY_TAU_HOURS = 24.0
"""Hours after which MemoryOS's recency term has decayed by a factor of e."""

READ_LIMIT = 1_000
"""Upper bound on one candidate read, kept well above every demo size."""

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "after",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "but",
        "by",
        "can",
        "did",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


def new_id(prefix: str) -> str:
    """Create an identifier in MemoryOS's own `prefix_hex` form.

    Args:
        prefix: Short kind name such as `"page"` or `"session"`.

    Returns:
        An id built from the prefix and eight hex characters.
    """

    return f"{prefix}_{uuid4().hex[:8]}"


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


def parse_timestamp(value: object, *, field: str) -> datetime:
    """Parse an ISO 8601 timestamp arriving from a JSON tool argument.

    Args:
        value: Timestamp text, which may end in `Z`.
        field: Field name used in error messages.

    Returns:
        The parsed timestamp in UTC.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the text is not ISO 8601 or carries no timezone.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO 8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from error
    return normalize_timestamp(parsed, field=field)


def required_text(value: object, *, field: str) -> str:
    """Return non-empty text or reject the input.

    Args:
        value: Candidate text.
        field: Field name used in error messages.

    Returns:
        The text with surrounding whitespace removed.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the text is empty.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty.")
    return text


def text_tuple(values: Iterable[Any], *, field: str) -> tuple[str, ...]:
    """Return a tuple of non-empty strings with repeats removed.

    Args:
        values: Candidate strings, normally arriving from JSON.
        field: Field name used in error messages.

    Returns:
        The strings in supplied order, each seen once.

    Raises:
        TypeError: If an entry is not a string.
        ValueError: If an entry is empty.
    """

    kept: list[str] = []
    for value in values:
        text = required_text(value, field=field)
        if text not in kept:
            kept.append(text)
    return tuple(kept)


def string_mapping(value: Any, *, field: str) -> dict[str, str]:
    """Return a flat string-to-string mapping copied from caller input.

    Args:
        value: Candidate mapping, or `None` for an empty one.
        field: Field name used in error messages.

    Returns:
        A new mapping with validated keys and values.

    Raises:
        TypeError: If `value` is not a mapping of strings to strings.
        ValueError: If a key or value is empty.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    return {required_text(key, field=f"{field} key"): required_text(item, field=f"{field} value") for key, item in value.items()}


def positive(value: object, *, field: str) -> int:
    """Return a positive integer or reject the input.

    Args:
        value: Candidate number.
        field: Field name used in error messages.

    Returns:
        The integer.

    Raises:
        TypeError: If `value` is not an integer.
        ValueError: If the integer is not greater than zero.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer.")
    if value < 1:
        raise ValueError(f"{field} must be greater than zero.")
    return value


def fraction(value: object, *, field: str) -> float:
    """Return a number between zero and one, inclusive.

    Args:
        value: Candidate number.
        field: Field name used in error messages.

    Returns:
        The value as a float.

    Raises:
        TypeError: If `value` is not a number.
        ValueError: If the value falls outside zero to one.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} must be between zero and one.")
    return float(value)


def non_negative(value: object, *, field: str) -> float:
    """Return a number that is zero or greater.

    Args:
        value: Candidate number.
        field: Field name used in error messages.

    Returns:
        The value as a float.

    Raises:
        TypeError: If `value` is not a number.
        ValueError: If the value is negative.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if float(value) < 0.0:
        raise ValueError(f"{field} must not be negative.")
    return float(value)


def tokenize(text: str) -> frozenset[str]:
    """Split text into the lowercase tokens similarity is measured over.

    Args:
        text: Free text.

    Returns:
        The distinct alphanumeric tokens in the text.
    """

    return frozenset(_TOKEN_PATTERN.findall(text.lower()))


def keywords(text: str) -> frozenset[str]:
    """Return the tokens MemoryOS would keep as a summary's keywords.

    Upstream a model writes the keyword list beside each summary it generates.
    Dropping a small stopword list is the deterministic stand-in, which is
    enough for the Jaccard term the segment score adds on top of similarity.

    Args:
        text: Free text.

    Returns:
        The content tokens in the text.
    """

    return frozenset(token for token in tokenize(text) if token not in _STOPWORDS and len(token) > 2)


def overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Return the share of one token set that appears in another.

    This stands in for cosine similarity between two normalized embeddings: it
    is one when every term is present and zero when none is, which is the range
    MemoryOS's thresholds are written against.

    Args:
        left: Tokens to look for, normally a query's.
        right: Tokens to look in.

    Returns:
        A value from zero to one.
    """

    if not left:
        return 0.0
    return len(left & right) / len(left)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return the Jaccard index MemoryOS uses to compare keyword sets.

    Args:
        left: First keyword set.
        right: Second keyword set.

    Returns:
        Intersection over union, or zero when both sets are empty.
    """

    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def time_decay(event: datetime, now: datetime, *, tau_hours: float = RECENCY_TAU_HOURS) -> float:
    """Return MemoryOS's exponential recency factor for one timestamp.

    Args:
        event: When the thing being scored last happened.
        now: Time the score is being taken at.
        tau_hours: Hours over which the factor falls by a factor of e.

    Returns:
        A value in `(0, 1]`, which is one at `now` and decays with age.

    Raises:
        ValueError: If `tau_hours` is not positive.
    """

    if tau_hours <= 0.0:
        raise ValueError("tau_hours must be greater than zero.")
    elapsed = max(0.0, (now - event).total_seconds()) / 3600.0
    return math.exp(-elapsed / tau_hours)


def segment_heat(
    *,
    visits: int,
    length: int,
    recency: float,
    alpha: float = HEAT_ALPHA,
    beta: float = HEAT_BETA,
    gamma: float = HEAT_GAMMA,
) -> float:
    """Return `H_segment`, the score that decides what reaches long-term memory.

    This is MemoryOS's formula unchanged:
    `H = alpha * N_visit + beta * L_interaction + gamma * R_recency`. What the
    lamp changes is where the three terms come from. Visits are counted on the
    segment, recency comes from the temporal index, and length is the number of
    pages the segment owns that no analysis has read yet -- which is the same
    quantity upstream keeps in `L_interaction` and resets to zero the moment it
    marks a segment's pages analyzed.

    Args:
        visits: `N_visit`, how often retrieval has reached this segment.
        length: `L_interaction`, how much unanalyzed dialogue it holds.
        recency: `R_recency`, the decay factor for its last visit.
        alpha: Weight on visits.
        beta: Weight on length.
        gamma: Weight on recency.

    Returns:
        The heat score, rounded to four places so runs agree exactly.
    """

    return round(alpha * visits + beta * length + gamma * recency, 4)


@dataclass(frozen=True, slots=True)
class Insight:
    """What one exchange would tell an analysis about the user.

    Upstream, promotion runs two model calls over a hot segment's pages: one
    rewrites the user profile and one splits what was learned into private user
    knowledge and assistant knowledge. The lamp has no model, so each exchange
    declares its own contribution when it is recorded, and the analysis is the
    collection of the contributions belonging to the pages being analyzed. That
    keeps the model's judgment at the boundary and makes the provenance edges
    exact, because every stored fact names the page that declared it.

    Attributes:
        profile: Profile traits this exchange would set or overwrite.
        user_knowledge: Facts about the user it would add.
        assistant_knowledge: Facts about doing the job it would add.
    """

    profile: dict[str, str] = field(default_factory=dict)
    user_knowledge: tuple[str, ...] = ()
    assistant_knowledge: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Return whether this exchange would tell an analysis nothing."""

        return not (self.profile or self.user_knowledge or self.assistant_knowledge)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form kept on a page.

        Returns:
            The declared contribution as plain JSON values.
        """

        return {
            "profile": dict(self.profile),
            "user_knowledge": list(self.user_knowledge),
            "assistant_knowledge": list(self.assistant_knowledge),
        }

    @classmethod
    def from_json(cls, value: Any, *, field_name: str = "insight") -> Insight:
        """Rebuild a declared contribution from its stored JSON form.

        Args:
            value: Mapping produced by `as_json`, or `None`.
            field_name: Field name used in error messages.

        Returns:
            The contribution, empty when nothing was declared.

        Raises:
            TypeError: If `value` is not a mapping.
        """

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError(f"{field_name} must be a mapping.")
        return cls(
            profile=string_mapping(value.get("profile"), field=f"{field_name} profile"),
            user_knowledge=text_tuple(value.get("user_knowledge") or (), field=f"{field_name} user_knowledge"),
            assistant_knowledge=text_tuple(value.get("assistant_knowledge") or (), field=f"{field_name} assistant_knowledge"),
        )


@dataclass(frozen=True, slots=True)
class Exchange:
    """One user turn and the assistant's reply, as MemoryOS is handed them.

    Attributes:
        user_input: What the user said.
        agent_response: What the assistant answered.
        occurred_at: When the exchange happened.
        topic: The themed summary a model would have written for it, which is
            what segment grouping compares against.
        continues: Whether this exchange continues the previous one, which
            upstream decides with a continuity model call.
        insight: What an analysis of this exchange would conclude.
    """

    user_input: str
    agent_response: str
    occurred_at: datetime
    topic: str
    continues: bool = False
    insight: Insight = field(default_factory=Insight)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this exchange.

        Returns:
            The exchange as plain JSON values.
        """

        return {
            "user_input": self.user_input,
            "agent_response": self.agent_response,
            "timestamp": format_timestamp(self.occurred_at),
            "topic": self.topic,
            "continues": self.continues,
            "insight": self.insight.as_json(),
        }


@dataclass(frozen=True, slots=True)
class QAPair:
    """One exchange while it is still resident in short-term memory.

    Attributes:
        block: Block holding the exchange.
        sequence: Position in the FIFO queue, oldest first.
        exchange: The exchange itself.
    """

    block: MemoryBlock
    sequence: int
    exchange: Exchange

    @property
    def id(self) -> str:
        """Return the block id, which is also the queue entry's identity."""

        return self.block.id

    @property
    def terms(self) -> frozenset[str]:
        """Return the tokens this exchange is matched on."""

        return tokenize(f"{self.exchange.user_input} {self.exchange.agent_response}")

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this queue entry.

        Returns:
            The queue position and the exchange it holds.
        """

        return {"sequence": self.sequence, **self.exchange.as_json()}


@dataclass(frozen=True, slots=True)
class Page:
    """One dialogue page after it has been paged out into mid-term memory.

    Attributes:
        block: Block holding the page.
        page_id: MemoryOS page id, an address rather than a scan target.
        segment_id: Id of the segment that owns it.
        exchange: The exchange the page was made from.
        analyzed: Whether an analysis has already read this page, which is
            what takes it out of its segment's `L_interaction`.
    """

    block: MemoryBlock
    page_id: str
    segment_id: str
    exchange: Exchange
    analyzed: bool

    @property
    def id(self) -> str:
        """Return the Genie block id of the page."""

        return self.block.id

    @property
    def occurred_at(self) -> datetime:
        """Return when the exchange on this page happened."""

        return self.exchange.occurred_at

    @property
    def terms(self) -> frozenset[str]:
        """Return the tokens a page-level match is measured over."""

        return tokenize(f"{self.exchange.user_input} {self.exchange.agent_response} {self.exchange.topic}")

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this page.

        Returns:
            The page id, its segment, its analysis state, and its exchange.
        """

        return {
            "page_id": self.page_id,
            "session_id": self.segment_id,
            "analyzed": self.analyzed,
            **self.exchange.as_json(),
        }


@dataclass(frozen=True, slots=True)
class Segment:
    """One mid-term session: a topic, the pages under it, and its heat.

    MemoryOS calls this a session and scores it as a segment. The pages are its
    children, so `length` is a read of the store rather than a counter that has
    to be kept in step with it.

    Attributes:
        block: Block holding the segment.
        segment_id: MemoryOS session id.
        summary: Themed summary the pages were grouped under.
        keywords: Keywords that came with the summary.
        visits: `N_visit`, incremented every time retrieval reaches it.
        lfu_count: `access_count_lfu`, the counter eviction reads and no
            analysis resets.
        created_at: When the segment was opened.
        last_visit_at: When it was last created, extended, or retrieved.
        recency: `R_recency` at the time it was read.
        pages: Its pages, oldest first.
    """

    block: MemoryBlock
    segment_id: str
    summary: str
    keywords: tuple[str, ...]
    visits: int
    lfu_count: int
    created_at: datetime
    last_visit_at: datetime
    recency: float
    pages: tuple[Page, ...]

    @property
    def id(self) -> str:
        """Return the Genie block id of the segment."""

        return self.block.id

    @property
    def length(self) -> int:
        """Return `L_interaction`: the pages here that no analysis has read."""

        return sum(1 for page in self.pages if not page.analyzed)

    @property
    def heat(self) -> float:
        """Return `H_segment` from the three terms as they stand now."""

        return segment_heat(visits=self.visits, length=self.length, recency=self.recency)

    @property
    def keyword_set(self) -> frozenset[str]:
        """Return the segment's keywords as a set for Jaccard comparison."""

        return frozenset(self.keywords)

    @property
    def summary_terms(self) -> frozenset[str]:
        """Return the tokens a segment-level match is measured over."""

        return tokenize(self.summary)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this segment.

        Returns:
            The session fields MemoryOS keeps, plus its computed heat.
        """

        return {
            "session_id": self.segment_id,
            "summary": self.summary,
            "summary_keywords": list(self.keywords),
            "N_visit": self.visits,
            "L_interaction": self.length,
            "R_recency": round(self.recency, 4),
            "H_segment": self.heat,
            "access_count_lfu": self.lfu_count,
            "timestamp": format_timestamp(self.created_at),
            "last_visit_time": format_timestamp(self.last_visit_at),
            "pages": [page.page_id for page in self.pages],
        }


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    """One entry in the user or assistant long-term knowledge base.

    Attributes:
        block: Block holding the entry.
        owner: Whose knowledge base it belongs to.
        knowledge: The fact itself.
        recorded_at: When promotion wrote it.
        sequence: Position in the capped log, oldest first.
        source_page_ids: Pages the fact was extracted from, which upstream
            does not keep.
    """

    block: MemoryBlock
    owner: KnowledgeOwner
    knowledge: str
    recorded_at: datetime
    sequence: int
    source_page_ids: tuple[str, ...]

    @property
    def id(self) -> str:
        """Return the Genie block id of the entry."""

        return self.block.id

    @property
    def terms(self) -> frozenset[str]:
        """Return the tokens a knowledge match is measured over."""

        return tokenize(self.knowledge)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this entry.

        Returns:
            The knowledge text with its owner, time, and provenance.
        """

        return {
            "id": self.block.id,
            "owner": self.owner,
            "knowledge": self.knowledge,
            "timestamp": format_timestamp(self.recorded_at),
            "source_page_ids": list(self.source_page_ids),
        }


@dataclass(frozen=True, slots=True)
class ProfileRevision:
    """One version of the user profile, as promotion left it.

    Attributes:
        block: Block holding this revision.
        revision: Logical revision number, starting at one.
        data: The profile traits after this analysis.
        recorded_at: When promotion wrote it.
        source_page_ids: Pages the analysis read.
    """

    block: MemoryBlock
    revision: int
    data: dict[str, str]
    recorded_at: datetime
    source_page_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this revision.

        Returns:
            The revision number, its traits, and where they came from.
        """

        return {
            "revision": self.revision,
            "profile": dict(self.data),
            "timestamp": format_timestamp(self.recorded_at),
            "source_page_ids": list(self.source_page_ids),
        }


@dataclass(frozen=True, slots=True)
class Analysis:
    """What one promotion decided to write into long-term memory.

    Attributes:
        profile: Traits to merge into the profile, or nothing to leave it.
        user_knowledge: Facts for the user's knowledge base, each with the
            page it came from.
        assistant_knowledge: Facts for the assistant's knowledge base.
        source_page_ids: Every page the analysis read.
    """

    profile: dict[str, str]
    user_knowledge: tuple[tuple[str, str], ...]
    assistant_knowledge: tuple[tuple[str, str], ...]
    source_page_ids: tuple[str, ...]

    @property
    def empty(self) -> bool:
        """Return whether the analysis found nothing worth promoting."""

        return not (self.profile or self.user_knowledge or self.assistant_knowledge)


@dataclass(frozen=True, slots=True)
class PageHit:
    """One mid-term page retrieval kept, with the segment it came from.

    Attributes:
        page: The page itself.
        segment_id: Session the page belongs to.
        segment_summary: That session's summary, which upstream shows beside
            the page as its conversation overview.
        segment_score: How well the session matched the query.
        score: How well the page itself matched.
    """

    page: Page
    segment_id: str
    segment_summary: str
    segment_score: float
    score: float

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this hit.

        Returns:
            The page, its session's overview, and both scores.
        """

        return {
            **self.page.as_json(),
            "session_summary": self.segment_summary,
            "session_score": round(self.segment_score, 4),
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """One knowledge entry retrieval kept.

    Attributes:
        item: The entry itself.
        score: How well it matched the query.
    """

    item: KnowledgeItem
    score: float

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this hit.

        Returns:
            The entry with the score it was kept on.
        """

        return {**self.item.as_json(), "score": round(self.score, 4)}


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    """Everything one retrieval gathered, tier by tier.

    This is the value MemoryOS's generation module turns into a prompt. The
    lamp stops here, because the step after it is a model call.

    Attributes:
        query: Text the retrieval ran for.
        retrieved_at: Time the retrieval was taken at.
        short_term: The whole FIFO queue, which upstream includes in full.
        pages: Mid-term pages that matched, best first.
        profile: The current profile traits, or nothing when none was written.
        profile_revision: Which revision those traits are, or zero.
        user_knowledge: Matching entries from the user's knowledge base.
        assistant_knowledge: Matching entries from the assistant's.
    """

    query: str
    retrieved_at: datetime
    short_term: tuple[QAPair, ...]
    pages: tuple[PageHit, ...]
    profile: dict[str, str]
    profile_revision: int
    user_knowledge: tuple[KnowledgeHit, ...]
    assistant_knowledge: tuple[KnowledgeHit, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this retrieval.

        Returns:
            One mapping per tier, in the order MemoryOS reads them.
        """

        return {
            "query": self.query,
            "retrieved_at": format_timestamp(self.retrieved_at),
            "short_term": [entry.as_json() for entry in self.short_term],
            "retrieved_pages": [hit.as_json() for hit in self.pages],
            "user_profile": dict(self.profile),
            "user_profile_revision": self.profile_revision,
            "retrieved_user_knowledge": [hit.as_json() for hit in self.user_knowledge],
            "retrieved_assistant_knowledge": [hit.as_json() for hit in self.assistant_knowledge],
        }


@dataclass(frozen=True, slots=True)
class Promotion:
    """One heat-triggered move from mid-term into long-term memory.

    Attributes:
        segment_id: Session that was hot enough to be analyzed.
        heat: Its heat when the check fired.
        threshold: Heat the check required.
        analyzed_page_ids: Pages the analysis read and marked.
        profile_revision: Revision the profile reached, or zero when the
            analysis changed no traits.
        user_knowledge: Entries written to the user's knowledge base.
        assistant_knowledge: Entries written to the assistant's.
        cooled_heat: The segment's heat once its visit and length terms had
            been reset.
        evictions: Entries a knowledge base dropped to make room for the new
            ones.
    """

    segment_id: str
    heat: float
    threshold: float
    analyzed_page_ids: tuple[str, ...]
    profile_revision: int
    user_knowledge: tuple[KnowledgeItem, ...]
    assistant_knowledge: tuple[KnowledgeItem, ...]
    cooled_heat: float
    evictions: tuple[Eviction, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this promotion.

        Returns:
            What was analyzed, what was written, and the heat before and after.
        """

        return {
            "session_id": self.segment_id,
            "H_segment": self.heat,
            "threshold": self.threshold,
            "analyzed_page_ids": list(self.analyzed_page_ids),
            "user_profile_revision": self.profile_revision,
            "user_knowledge": [item.as_json() for item in self.user_knowledge],
            "assistant_knowledge": [item.as_json() for item in self.assistant_knowledge],
            "cooled_H_segment": self.cooled_heat,
            "evictions": [eviction.as_json() for eviction in self.evictions],
        }


@dataclass(frozen=True, slots=True)
class Eviction:
    """One page-out or drop the tiers performed on their own.

    Attributes:
        tier: Tier the eviction happened in.
        reason: Why it happened, in MemoryOS's own terms.
        item_id: Identifier of what left, a page or a session id.
        detail: Short readable note about what it held.
    """

    tier: str
    reason: str
    item_id: str
    detail: str

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this eviction.

        Returns:
            Which tier lost what, and why.
        """

        return {"tier": self.tier, "reason": self.reason, "id": self.item_id, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AddResult:
    """Everything one `add_memory` call set in motion.

    A single call can page out the oldest exchange, open or extend a segment,
    evict the coldest segment, and promote the hottest one. Returning all of it
    is what makes the tiering inspectable instead of implied.

    Attributes:
        stored: The exchange as it now sits in short-term memory.
        paged_out: Pages the overflow moved into mid-term memory.
        promotion: The long-term update the heat check triggered, if any.
        evictions: Anything the tiers dropped to stay inside capacity.
    """

    stored: QAPair
    paged_out: tuple[Page, ...]
    promotion: Promotion | None
    evictions: tuple[Eviction, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this call's effects.

        Returns:
            The stored exchange and every tier movement it caused.
        """

        return {
            "stored": self.stored.as_json(),
            "paged_out": [page.as_json() for page in self.paged_out],
            "promotion": self.promotion.as_json() if self.promotion is not None else None,
            "evictions": [eviction.as_json() for eviction in self.evictions],
        }


def exchange_from_json(value: Mapping[str, Any]) -> Exchange:
    """Rebuild an exchange from JSON tool arguments.

    Args:
        value: Mapping with the fields `as_json` produces.

    Returns:
        The exchange.

    Raises:
        TypeError: If a field has the wrong runtime type.
        ValueError: If required text is missing.
    """

    return Exchange(
        user_input=required_text(value.get("user_input"), field="user_input"),
        agent_response=required_text(value.get("agent_response"), field="agent_response"),
        occurred_at=parse_timestamp(value.get("timestamp"), field="timestamp"),
        topic=required_text(value.get("topic"), field="topic"),
        continues=bool(value.get("continues", False)),
        insight=Insight.from_json(value.get("insight")),
    )


def owner_of(value: object, *, field: str = "owner") -> KnowledgeOwner:
    """Return a validated knowledge-base owner.

    Args:
        value: Candidate owner name.
        field: Field name used in error messages.

    Returns:
        Either `"user"` or `"assistant"`.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the name is not a knowledge-base owner.
    """

    owner = required_text(value, field=field)
    if owner not in KNOWLEDGE_OWNERS:
        raise ValueError(f"{field} must be one of {', '.join(KNOWLEDGE_OWNERS)}.")
    return "user" if owner == USER else "assistant"


def ordered_topics(pages: Sequence[Page]) -> tuple[str, ...]:
    """Return the distinct topics a run of pages was grouped under.

    Args:
        pages: Pages in the order they were paged out.

    Returns:
        Each topic once, in first-seen order.
    """

    return text_tuple((page.exchange.topic for page in pages), field="topic")
