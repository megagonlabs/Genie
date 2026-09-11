"""Four-way recall over facts and observations, fused and capped at a budget.

Hindsight does not answer a query from one index. It runs four retrievers at
once - semantic, keyword, graph, and temporal - fuses their ranked lists with
reciprocal rank fusion, then fills a token budget rather than returning a fixed
number of rows. Observations ride along with raw facts, so a consolidated belief
can outrank the sentences it was built from.

Three of the four retrievers are Genie calls, which is the point of showing this
recipe at all:

- `keyword` issues one `MemoryHandle.search` per query term, so the portable
  query layer does the matching.
- `graph` walks the `PropertyGraph` outward from an entity, which reaches the
  facts that mention it, the facts those are linked to, and the observations
  derived from them.
- `temporal` reads the `TemporalCollection` on both of its axes: facts learned
  inside the window, and facts describing a period that overlaps it.
- Only `similarity` is hand-written, as a visible stand-in for embeddings.

Fusion and the budget are the lamp's own code, because ranking is exactly what
Genie leaves to applications. Hindsight's cross-encoder reranker and its
recency, temporal-proximity, and proof-count boosts are not reproduced. The
README lists them as omissions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from examples.lamps.hindsight.bank import HindsightMemoryBank
from examples.lamps.hindsight.model import (
    MEMORY_TYPE,
    OBSERVATION_REVISION_TYPE,
    RECALL_TYPES,
    SEARCH_LIMIT,
    RecallHit,
    RecallType,
    estimate_tokens,
    normalize_timestamp,
    required_text,
    tokenize,
)
from examples.lamps.hindsight.observations import ConsolidationEngine
from genie import MemoryBlock
from genie.structures import ValidityInterval

RETRIEVERS = ("similarity", "keyword", "graph", "temporal")
DEFAULT_MAX_TOKENS = 80

_RRF_CONSTANT = 60
_MAX_GRAPH_DEPTH = 2


@dataclass(frozen=True, slots=True)
class RecallWindow:
    """The half-open time range a temporal query asks about.

    Attributes:
        start: Inclusive lower bound.
        end: Exclusive upper bound.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Normalize both bounds to UTC and reject a reversed range."""

        start = normalize_timestamp(self.start, field="window start")
        end = normalize_timestamp(self.end, field="window end")
        if end <= start:
            raise ValueError("Recall window end must be later than its start.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def as_interval(self) -> ValidityInterval:
        """Return this window as a Genie validity interval."""

        return ValidityInterval(valid_at=self.start, invalid_at=self.end)


@dataclass(frozen=True, slots=True)
class RecallResponse:
    """What one recall returned, and what the budget left out.

    Attributes:
        query: Query text that was run.
        hits: Selected results, highest fused score first.
        tokens_used: Estimated tokens the selected results cost.
        max_tokens: Budget the selection was capped at.
        dropped: Number of ranked candidates the budget excluded.
    """

    query: str
    hits: tuple[RecallHit, ...]
    tokens_used: int
    max_tokens: int
    dropped: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One retrievable item before fusion.

    Attributes:
        block: Fact or observation revision block.
        text: Text the retrievers score.
        kind: `world`, `experience`, or `observation`.
        tokens: Token set used by the similarity retriever.
        cost: Estimated token cost charged to the recall budget.
        order: Stable tie-break key. Ranking must never fall back on
            `MemoryBlock.id`, which is a fresh UUID on every run: an id-ordered
            tie would change a candidate's rank inside a retriever, change its
            reciprocal-rank contribution, and make scores differ run to run.
    """

    block: MemoryBlock
    text: str
    kind: RecallType
    tokens: frozenset[str]
    cost: int
    order: str


class HindsightRecall:
    """The lamp's one recall recipe: four retrievers, fused, then budgeted."""

    def __init__(self, bank: HindsightMemoryBank, consolidation: ConsolidationEngine) -> None:
        """Create recall over one bank and its consolidated beliefs.

        Args:
            bank: Bank holding the facts and the structures searched.
            consolidation: Engine supplying current observations.
        """

        self.bank = bank
        self.consolidation = consolidation

    def recall(
        self,
        query: str,
        *,
        entity: str | None = None,
        types: Sequence[str] = (),
        tags: Sequence[str] = (),
        window: RecallWindow | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        limit: int = 10,
    ) -> RecallResponse:
        """Run all four retrievers and return the fused results.

        Args:
            query: Natural-language query.
            entity: Optional entity anchoring graph traversal. Without one,
                entities named in the query are used.
            types: Optional subset of `world`, `experience`, `observation`.
            tags: Tags every returned item must carry.
            window: Optional time range enabling the temporal retriever.
            max_tokens: Budget the selected results must fit inside.
            limit: Maximum results, applied before the budget.

        Returns:
            The fused, budgeted results.

        Raises:
            ValueError: If the query is empty or a bound is negative.
        """

        text = required_text(query, field="query")
        if limit < 0 or max_tokens < 0:
            raise ValueError("Recall limit and max_tokens must be non-negative.")
        selected_types = _validated_types(types)
        candidates = self._candidates(types=selected_types, tags=tuple(tags))
        query_tokens = tokenize(text)
        rankings = {
            "similarity": _similarity_ranking(query_tokens, candidates),
            "keyword": self._keyword_ranking(query_tokens, candidates),
            "graph": self._graph_ranking(text, candidates, entity=entity),
            "temporal": self._temporal_ranking(candidates, window=window),
        }
        fused = _reciprocal_rank_fusion(rankings.values())
        ordered = sorted(
            fused.items(),
            key=lambda item: (-item[1], candidates[item[0]].order),
        )[:limit]
        return _fit_budget(
            query=text,
            ordered=ordered,
            candidates=candidates,
            rankings=rankings,
            max_tokens=max_tokens,
        )

    def _candidates(self, *, types: frozenset[str], tags: tuple[str, ...]) -> dict[str, _Candidate]:
        """Collect the valid facts and current observations recall may return."""

        required_tags = set(tags)
        candidates: dict[str, _Candidate] = {}
        for fact in self.bank.facts():
            if fact.fact_type not in types or not required_tags.issubset(fact.tags):
                continue
            candidates[fact.id] = _Candidate(
                block=fact.block,
                text=fact.text,
                kind=fact.fact_type,
                tokens=tokenize(f"{fact.text} {fact.context} {' '.join(fact.entities)}"),
                cost=estimate_tokens(fact.text),
                order=f"fact|{fact.text}",
            )
        if "observation" not in types:
            return candidates
        for observation in self.consolidation.observations():
            if not required_tags.issubset(observation.tags):
                continue
            candidates[observation.id] = _Candidate(
                block=observation.block,
                text=observation.text,
                kind="observation",
                tokens=tokenize(observation.text),
                cost=estimate_tokens(observation.text),
                order=f"observation|{observation.claim}",
            )
        return candidates

    def _keyword_ranking(
        self,
        query_tokens: frozenset[str],
        candidates: dict[str, _Candidate],
    ) -> list[str]:
        """Rank candidates by how many query terms Genie search matched.

        One `MemoryHandle.search` per term is a transparent stand-in for BM25:
        the portable query layer does the matching, and the lamp only counts
        which candidates each term reached.
        """

        matches: dict[str, int] = {}
        for term in sorted(query_tokens):
            for result in self.bank.memory.search(
                text=term,
                types=[MEMORY_TYPE, OBSERVATION_REVISION_TYPE],
                limit=SEARCH_LIMIT,
            ):
                if result.block.id in candidates:
                    matches[result.block.id] = matches.get(result.block.id, 0) + 1
        return sorted(matches, key=lambda item: (-matches[item], candidates[item].order))

    def _graph_ranking(
        self,
        query: str,
        candidates: dict[str, _Candidate],
        *,
        entity: str | None,
    ) -> list[str]:
        """Rank candidates by how few hops separate them from a named entity.

        The walk crosses three relations without knowing it: the mention edges
        into facts about the entity, the associative edges between those facts,
        and the lineage edges out to the observations built from them.
        """

        distances: dict[str, int] = {}
        for anchor in self._anchors(query, entity=entity):
            for path in self.bank.structures.graph.walk(
                anchor,
                direction="both",
                max_depth=_MAX_GRAPH_DEPTH,
                limit=SEARCH_LIMIT,
            ):
                for depth, block_id in enumerate(path.block_ids):
                    if block_id in candidates and depth < distances.get(block_id, depth + 1):
                        distances[block_id] = depth
        return sorted(distances, key=lambda item: (distances[item], candidates[item].order))

    def _temporal_ranking(
        self,
        candidates: dict[str, _Candidate],
        *,
        window: RecallWindow | None,
    ) -> list[str]:
        """Rank candidates by both temporal axes inside a window.

        Facts learned in the window and facts describing a period that overlaps
        it both qualify, which is the distinction Hindsight draws between when
        something happened and when the bank found out.
        """

        if window is None:
            return []
        distances: dict[str, float] = {}
        for fact in self.bank.learned_between(window.start, window.end):
            if fact.id in candidates:
                distances[fact.id] = (fact.learned_at - window.start).total_seconds()
        for fact in self.bank.describing(window.as_interval()):
            if fact.id not in candidates or fact.occurred is None:
                continue
            offset = abs((fact.occurred.valid_at - window.start).total_seconds())
            distances[fact.id] = min(distances.get(fact.id, offset), offset)
        return sorted(distances, key=lambda item: (distances[item], candidates[item].order))

    def _anchors(self, query: str, *, entity: str | None) -> list[MemoryBlock]:
        """Return the entity blocks graph traversal should start from."""

        if entity is not None:
            resolved = self.bank.entity(entity)
            return [resolved.block] if resolved is not None else []
        query_tokens = tokenize(query)
        return [
            candidate.block
            for candidate in self.bank.entities()
            if tokenize(candidate.name) & query_tokens
        ]


def _fit_budget(
    *,
    query: str,
    ordered: Sequence[tuple[str, float]],
    candidates: dict[str, _Candidate],
    rankings: dict[str, list[str]],
    max_tokens: int,
) -> RecallResponse:
    """Take fused results in order until the token budget is spent."""

    hits: list[RecallHit] = []
    used = 0
    dropped = 0
    for block_id, score in ordered:
        candidate = candidates[block_id]
        if used + candidate.cost > max_tokens:
            dropped += 1
            continue
        used += candidate.cost
        hits.append(
            RecallHit(
                block=candidate.block,
                text=candidate.text,
                kind=candidate.kind,
                score=round(score, 6),
                retrievers=tuple(name for name, ranked in rankings.items() if block_id in ranked),
                tokens=candidate.cost,
            )
        )
    return RecallResponse(
        query=query,
        hits=tuple(hits),
        tokens_used=used,
        max_tokens=max_tokens,
        dropped=dropped,
    )


def _reciprocal_rank_fusion(rankings: Iterable[Sequence[str]]) -> dict[str, float]:
    """Fuse ranked id lists with reciprocal rank fusion.

    Args:
        rankings: One ranked id list per retriever.

    Returns:
        Fused score per id that appeared in at least one ranking.
    """

    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, block_id in enumerate(ranking, start=1):
            fused[block_id] = fused.get(block_id, 0.0) + 1.0 / (_RRF_CONSTANT + rank)
    return fused


def _similarity_ranking(
    query_tokens: frozenset[str],
    candidates: dict[str, _Candidate],
) -> list[str]:
    """Rank candidates by token-set cosine overlap with the query."""

    matched = [
        (block_id, score)
        for block_id, candidate in candidates.items()
        if (score := _cosine(query_tokens, candidate.tokens)) > 0.0
    ]
    return [
        block_id
        for block_id, _ in sorted(
            matched,
            key=lambda item: (-item[1], candidates[item[0]].order),
        )
    ]


def _cosine(left: frozenset[str], right: frozenset[str]) -> float:
    """Return cosine similarity between two token sets."""

    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _validated_types(types: Sequence[str]) -> frozenset[str]:
    """Return the recall types to include, defaulting to all three."""

    if not types:
        return RECALL_TYPES
    unknown = sorted(set(types).difference(RECALL_TYPES))
    if unknown:
        raise ValueError(f"Unsupported recall types: {', '.join(unknown)}.")
    return frozenset(types)
