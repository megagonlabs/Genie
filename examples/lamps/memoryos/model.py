"""Value types, constants, and the shared calculations of the MemoryOS lamp.

MemoryOS copies its shape from an operating system:

- **Page:** a dialogue page is the unit that moves between tiers.
- **Short-term memory:** the fast tier. It holds only the newest few exchanges.
- **Mid-term memory:** the paged tier. It groups pages into topic segments.
- **Long-term memory:** the slow tier. It keeps the information that an analysis
  found important.
- **Heat, not age, decides promotion:** the score of a segment increases when a
  retrieval visits it and when it gets pages. The score decreases while nothing
  uses the segment.

3 parts of this module let the rest of the lamp stay small:

- **Block types:** they keep the contents of the 4 tiers separate.
- **Records:** the tiers return these records. Each record has a JSON form, so
  the same values can cross a tool boundary.
- **`segment_heat` and `time_decay`:** the MemoryOS formulas. They are only in
  this module, so the updater and the retriever calculate heat the same way.

This module makes no Genie calls. It imports only the `MemoryBlock` and
`MemoryAddress` types from Genie, and it imports no other lamp module. So each
other module can import from it.
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
"""Weight that MemoryOS gives to the number of visits to a segment."""

HEAT_BETA = 1.0
"""Weight that MemoryOS gives to the quantity of dialogue in a segment that no analysis has read."""

HEAT_GAMMA = 1.0
"""Weight that MemoryOS gives to the recency of the last visit to a segment."""

RECENCY_TAU_HOURS = 24.0
"""Number of hours after which the MemoryOS recency term decreases by a factor of e."""

READ_LIMIT = 1_000
"""Maximum number of candidates in one read. This value is much larger than each demo size."""

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
    """Create an identifier in the MemoryOS `prefix_hex` form.

    Args:
        prefix: Short kind name, for example `"page"` or `"session"`.

    Returns:
        An id made from the prefix and 8 hex characters.
    """

    return f"{prefix}_{uuid4().hex[:8]}"


def normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Return a timestamp with a timezone, converted to UTC.

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
        value: Timestamp with a timezone.

    Returns:
        An ISO 8601 string that ends in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object, *, field: str) -> datetime:
    """Parse an ISO 8601 timestamp from a JSON tool argument.

    Args:
        value: Timestamp text. It can end in `Z`.
        field: Field name for error messages.

    Returns:
        The parsed timestamp in UTC.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the text is not ISO 8601 or has no timezone.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO 8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from error
    return normalize_timestamp(parsed, field=field)


def required_text(value: object, *, field: str) -> str:
    """Return text that is not empty, or refuse the input.

    Args:
        value: Candidate text.
        field: Field name for error messages.

    Returns:
        The text without whitespace at the start and the end.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the text is empty or contains only whitespace.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty.")
    return text


def text_tuple(values: Iterable[Any], *, field: str) -> tuple[str, ...]:
    """Return a tuple of strings that are not empty, with no repeated strings.

    Args:
        values: Candidate strings, usually from JSON.
        field: Field name for error messages.

    Returns:
        The strings without whitespace at the start and the end, in the given
        order, each string once.

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
    """Return a copy of caller input as a flat mapping from strings to strings.

    Args:
        value: Candidate mapping, or `None` for an empty mapping.
        field: Field name for error messages.

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
    """Return a positive integer, or refuse the input.

    Args:
        value: Candidate number.
        field: Field name for error messages.

    Returns:
        The integer.

    Raises:
        TypeError: If `value` is not an integer, or is a `bool`.
        ValueError: If the integer is not greater than zero.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer.")
    if value < 1:
        raise ValueError(f"{field} must be greater than zero.")
    return value


def fraction(value: object, *, field: str) -> float:
    """Return a number from zero to one, inclusive.

    Args:
        value: Candidate number.
        field: Field name for error messages.

    Returns:
        The value as a float.

    Raises:
        TypeError: If `value` is not a number, or is a `bool`.
        ValueError: If the value is not in the range from zero to one.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} must be between zero and one.")
    return float(value)


def non_negative(value: object, *, field: str) -> float:
    """Return a number that is zero or more.

    Args:
        value: Candidate number.
        field: Field name for error messages.

    Returns:
        The value as a float.

    Raises:
        TypeError: If `value` is not a number, or is a `bool`.
        ValueError: If the value is negative.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a number.")
    if float(value) < 0.0:
        raise ValueError(f"{field} must not be negative.")
    return float(value)


def tokenize(text: str) -> frozenset[str]:
    """Split text into the lowercase tokens that the similarity measure compares.

    Args:
        text: Free text.

    Returns:
        The distinct alphanumeric tokens in the text.
    """

    return frozenset(_TOKEN_PATTERN.findall(text.lower()))


def keywords(text: str) -> frozenset[str]:
    """Return the tokens that MemoryOS would keep as the keywords of a summary.

    - **Upstream:** a model writes the keyword list next to each summary that it
      makes.
    - **Here:** the function removes the tokens in a small stopword list and the
      tokens of 2 characters or fewer. This deterministic replacement is
      sufficient for the Jaccard term that the segment score adds to the
      similarity.

    Args:
        text: Free text.

    Returns:
        The content tokens in the text.
    """

    return frozenset(token for token in tokenize(text) if token not in _STOPWORDS and len(token) > 2)


def overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Return the fraction of the tokens in one set that are also in another set.

    This value replaces the cosine similarity between 2 normalized embeddings.
    It is one when each term is present, and zero when no term is present. The
    MemoryOS thresholds use this range.

    Args:
        left: Tokens to find, usually the tokens of a query.
        right: Tokens to search in.

    Returns:
        A value from zero to one. The value is zero when `left` is empty.
    """

    if not left:
        return 0.0
    return len(left & right) / len(left)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return the Jaccard index that MemoryOS uses to compare keyword sets.

    Args:
        left: First keyword set.
        right: Second keyword set.

    Returns:
        The size of the intersection divided by the size of the union, or zero
        when both sets are empty.
    """

    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def time_decay(event: datetime, now: datetime, *, tau_hours: float = RECENCY_TAU_HOURS) -> float:
    """Return the MemoryOS exponential recency factor for one timestamp.

    Args:
        event: Time of the last occurrence of the scored item.
        now: Time of the score calculation.
        tau_hours: Number of hours in which the factor decreases by a factor
            of e.

    Returns:
        A value in `(0, 1]`. It is one at `now` and decreases with age. An
        `event` after `now` also gives one.

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
    """Return `H_segment`, the score that decides what goes to long-term memory.

    The formula is the MemoryOS formula with no change:
    `H = alpha * N_visit + beta * L_interaction + gamma * R_recency`.

    The lamp changes only the sources of the 3 terms:

    - **Visits:** the segment block stores the visit count.
    - **Recency:** comes from the temporal index.
    - **Length:** the number of pages of the segment that no analysis has read.
      Upstream keeps the same quantity in `L_interaction`. Upstream sets it to
      zero at the same time that it marks the pages of a segment as analyzed.

    Args:
        visits: `N_visit`, the number of times that retrieval reached this
            segment.
        length: `L_interaction`, the quantity of dialogue in the segment that no
            analysis has read.
        recency: `R_recency`, the decay factor for its last visit.
        alpha: Weight on visits.
        beta: Weight on length.
        gamma: Weight on recency.

    Returns:
        The heat score, rounded to 4 decimal places, so that all runs give the
        same result.
    """

    return round(alpha * visits + beta * length + gamma * recency, 4)


@dataclass(frozen=True, slots=True)
class Insight:
    """What one exchange would tell an analysis about the user.

    - **Upstream:** promotion makes 2 model calls on the pages of a hot segment.
      One call writes the user profile again. The other call splits what the
      pages showed into private user knowledge and assistant knowledge.
    - **Here:** the lamp has no model. So each exchange declares its own
      contribution when the lamp records it. The analysis collects the
      contributions of the pages that it analyzes.
    - **Result:** the judgment of the model stays at the boundary. The
      provenance edges are exact, because each stored fact names the page that
      declared it.

    Attributes:
        profile: Profile traits that this exchange would set or replace.
        user_knowledge: Facts about the user that it would add.
        assistant_knowledge: Facts about how to do the job that it would add.
    """

    profile: dict[str, str] = field(default_factory=dict)
    user_knowledge: tuple[str, ...] = ()
    assistant_knowledge: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Return `True` if this exchange would tell an analysis nothing."""

        return not (self.profile or self.user_knowledge or self.assistant_knowledge)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form that a page keeps.

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
            value: Mapping from `as_json`, or `None`.
            field_name: Field name for error messages.

        Returns:
            The contribution. It is empty when the exchange declared nothing.

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
    """One user turn and the reply of the assistant, as MemoryOS receives them.

    Attributes:
        user_input: What the user said.
        agent_response: What the assistant answered.
        occurred_at: When the exchange occurred.
        topic: The themed summary that a model would write for it. The lamp
            compares this summary when it puts pages into segments.
        continues: `True` if this exchange continues the previous exchange.
            Upstream, a continuity model call decides this.
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
    """One exchange while it is still in short-term memory.

    Attributes:
        block: Block that holds the exchange.
        sequence: Position in the FIFO queue, oldest first.
        exchange: The exchange.
    """

    block: MemoryBlock
    sequence: int
    exchange: Exchange

    @property
    def id(self) -> str:
        """Return the block id, which is also the identity of the queue entry."""

        return self.block.id

    @property
    def terms(self) -> frozenset[str]:
        """Return the tokens of the user input and the assistant reply, which a match compares."""

        return tokenize(f"{self.exchange.user_input} {self.exchange.agent_response}")

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this queue entry.

        Returns:
            The queue position and the exchange in it.
        """

        return {"sequence": self.sequence, **self.exchange.as_json()}


@dataclass(frozen=True, slots=True)
class Page:
    """One dialogue page after a page-out to mid-term memory.

    Attributes:
        block: Block that holds the page.
        page_id: MemoryOS page id. It is an address, not a scan target.
        segment_id: Id of the segment that owns the page.
        exchange: The exchange that the page was made from.
        analyzed: `True` if an analysis already read this page. An analyzed
            page does not count toward the `L_interaction` of its segment.
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
        """Return the time of the exchange on this page."""

        return self.exchange.occurred_at

    @property
    def terms(self) -> frozenset[str]:
        """Return the tokens that a page-level match compares: the user input, the assistant reply, and the topic."""

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
    children, so `length` reads the store. It is not a counter that the lamp
    keeps in step with the store.

    Attributes:
        block: Block that holds the segment.
        segment_id: MemoryOS session id.
        summary: Themed summary that the pages were grouped under.
        keywords: Keywords that came with the summary.
        visits: `N_visit`. Each retrieval that reaches the segment adds one,
            and promotion sets it to zero.
        lfu_count: `access_count_lfu`, the counter that eviction reads. No
            analysis sets it to zero.
        created_at: When the segment was opened.
        last_visit_at: When the segment was last created, extended, retrieved,
            or cooled after a promotion.
        recency: `R_recency` at the time of the read.
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
        """Return `L_interaction`: the number of pages here that no analysis has read."""

        return sum(1 for page in self.pages if not page.analyzed)

    @property
    def heat(self) -> float:
        """Return `H_segment` from the current values of the 3 terms."""

        return segment_heat(visits=self.visits, length=self.length, recency=self.recency)

    @property
    def keyword_set(self) -> frozenset[str]:
        """Return the keywords of the segment as a set for the Jaccard comparison."""

        return frozenset(self.keywords)

    @property
    def summary_terms(self) -> frozenset[str]:
        """Return the tokens that a segment-level match compares."""

        return tokenize(self.summary)

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this segment.

        Returns:
            The session fields that MemoryOS keeps, and the calculated heat.
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
    """One entry in the long-term knowledge base of the user or the assistant.

    Attributes:
        block: Block that holds the entry.
        owner: The owner of the knowledge base that contains the entry.
        knowledge: The fact.
        recorded_at: When promotion wrote it.
        sequence: Position in the log of the knowledge base, oldest first.
        source_page_ids: Ids of the pages that the analysis took the fact from.
            Upstream does not keep this data.
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
        """Return the tokens that a knowledge match compares."""

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
    """One version of the user profile, as promotion wrote it.

    Attributes:
        block: Block that holds this revision.
        revision: Logical revision number. The first revision is one.
        data: The profile traits after this analysis.
        recorded_at: When promotion wrote it.
        source_page_ids: Ids of the pages that the analysis read.
    """

    block: MemoryBlock
    revision: int
    data: dict[str, str]
    recorded_at: datetime
    source_page_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this revision.

        Returns:
            The revision number, its traits, and their source pages.
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
        profile: Traits to merge into the profile. An empty mapping keeps the
            profile as it is.
        user_knowledge: Facts for the knowledge base of the user. Each fact has
            the id of the page that it came from.
        assistant_knowledge: Facts for the knowledge base of the assistant, in
            the same form.
        source_page_ids: Ids of all pages that the analysis read.
    """

    profile: dict[str, str]
    user_knowledge: tuple[tuple[str, str], ...]
    assistant_knowledge: tuple[tuple[str, str], ...]
    source_page_ids: tuple[str, ...]

    @property
    def empty(self) -> bool:
        """Return `True` if the analysis found nothing to promote."""

        return not (self.profile or self.user_knowledge or self.assistant_knowledge)


@dataclass(frozen=True, slots=True)
class PageHit:
    """One mid-term page that retrieval kept, with the segment that it came from.

    Attributes:
        page: The page.
        segment_id: Session that the page belongs to.
        segment_summary: The summary of that session. Upstream shows it next to
            the page as the conversation overview.
        segment_score: How well the session matched the query.
        score: How well the page matched the query.
    """

    page: Page
    segment_id: str
    segment_summary: str
    segment_score: float
    score: float

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this hit.

        Returns:
            The page, the overview of its session, and both scores.
        """

        return {
            **self.page.as_json(),
            "session_summary": self.segment_summary,
            "session_score": round(self.segment_score, 4),
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """One knowledge entry that retrieval kept.

    Attributes:
        item: The entry.
        score: How well it matched the query.
    """

    item: KnowledgeItem
    score: float

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this hit.

        Returns:
            The entry and its score.
        """

        return {**self.item.as_json(), "score": round(self.score, 4)}


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    """All context that one retrieval collected, tier by tier.

    The MemoryOS generation module turns this value into a prompt. The lamp
    stops here, because the next step is a model call.

    Attributes:
        query: Text that the retrieval used.
        retrieved_at: Time of the retrieval.
        short_term: The full FIFO queue. Upstream puts all of it in the prompt.
        pages: Mid-term pages that matched, best first.
        profile: The current profile traits, or an empty mapping when no
            analysis wrote a profile.
        profile_revision: The revision number of those traits, or zero.
        user_knowledge: Entries from the knowledge base of the user that match
            the query.
        assistant_knowledge: Entries from the knowledge base of the assistant
            that match the query.
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
            One mapping for each tier, in the order that MemoryOS reads them.
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
    """One move from mid-term memory into long-term memory, caused by heat.

    Attributes:
        segment_id: Session that was hot enough for an analysis.
        heat: Its heat when the check started the promotion.
        threshold: Heat that the check required.
        analyzed_page_ids: Ids of the pages that the analysis read and marked.
        profile_revision: Revision number of the profile after the analysis,
            or zero when the analysis set no traits.
        user_knowledge: Entries that the promotion wrote to the knowledge base
            of the user.
        assistant_knowledge: Entries that the promotion wrote to the knowledge
            base of the assistant.
        cooled_heat: The heat of the segment after the reset of its visit and
            length terms.
        evictions: Entries that a knowledge base removed to make room for the
            new entries.
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
            What the analysis read, what it wrote, and the heat before and
            after.
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
    """One removal that a tier made on its own to stay inside its capacity.

    Attributes:
        tier: Tier where the eviction occurred.
        reason: Why it occurred, in MemoryOS terms.
        item_id: Identifier of the item that left: a session id for a mid-term
            segment, or a block id for a knowledge entry.
        detail: Short readable note about what the item held.
    """

    tier: str
    reason: str
    item_id: str
    detail: str

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this eviction.

        Returns:
            The tier, the item that left it, and the reason.
        """

        return {"tier": self.tier, "reason": self.reason, "id": self.item_id, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AddResult:
    """All effects that one `add_memory` call started.

    One call can page out the oldest exchange, open or extend a segment, evict
    the coldest segment, and promote the hottest segment. The result contains
    all of these effects, so a caller can inspect the tiers from the result
    alone.

    Attributes:
        stored: The exchange as it is now in short-term memory.
        paged_out: Pages that the overflow moved into mid-term memory.
        promotion: The long-term update that the heat check started, or `None`.
        evictions: All items that the tiers removed to stay inside their
            capacity.
    """

    stored: QAPair
    paged_out: tuple[Page, ...]
    promotion: Promotion | None
    evictions: tuple[Eviction, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the JSON-compatible form of this call's effects.

        Returns:
            The stored exchange and each tier movement that the call caused.
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
        value: Mapping with the fields that `as_json` makes.

    Returns:
        The exchange.

    Raises:
        TypeError: If a field has the wrong runtime type.
        ValueError: If required text is empty or absent, or the timestamp is
            not ISO 8601 with a timezone.
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
    """Return a validated owner of a knowledge base.

    Args:
        value: Candidate owner name.
        field: Field name for error messages.

    Returns:
        `"user"` or `"assistant"`.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the name is not an owner of a knowledge base.
    """

    owner = required_text(value, field=field)
    if owner not in KNOWLEDGE_OWNERS:
        raise ValueError(f"{field} must be one of {', '.join(KNOWLEDGE_OWNERS)}.")
    return "user" if owner == USER else "assistant"


def ordered_topics(pages: Sequence[Page]) -> tuple[str, ...]:
    """Return the distinct topics of a sequence of pages.

    Args:
        pages: Pages in page-out order.

    Returns:
        Each topic once, in the order of first occurrence.
    """

    return text_tuple((page.exchange.topic for page in pages), field="topic")
