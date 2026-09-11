"""Hybrid fact retrieval for the Graphiti-inspired lamp.

Graphiti retrieves by running several retrievers over the graph and fusing their
ranked results, rather than by hitting one index. Its default recipe,
`EDGE_HYBRID_SEARCH_RRF`, combines full-text search, embedding similarity, and
breadth-first graph traversal over facts under reciprocal rank fusion. This
module implements that one recipe.

Ranking is exactly what Genie leaves to applications, so only the retrievers
reach into the library:

- `keyword` issues one `MemoryHandle.search` per query term.
- `graph` walks `PropertyGraph` outward from a center entity.
- `similarity` is token-set cosine overlap, a visible stand-in for embeddings.
- The bi-temporal filters are the other Genie-facing part. They narrow the
  candidate set through the same temporal collections the graph stores facts in.

Graphiti's other recipes and its MMR, node-distance, episode-mention, and
cross-encoder rerankers are deliberately not reproduced. The lamp README lists
them as omissions.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from examples.lamps.graphiti.model import FACT_TYPE, SEARCH_LIMIT, Entity, TemporalFact
from examples.lamps.graphiti.temporal_graph import TemporalKnowledgeGraph

RECIPE_NAME = "EDGE_HYBRID_SEARCH_RRF"
RETRIEVERS = ("keyword", "similarity", "graph")

_RRF_CONSTANT = 60
_MAX_GRAPH_DEPTH = 3
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Restrictions applied before retrieval runs.

    `as_of` and `known_at` are the bi-temporal filters: the first keeps facts
    that were true then, the second keeps facts the graph had on record then.

    Attributes:
        as_of: Optional effective time facts must have been true at.
        known_at: Optional ingestion time facts must have been on record at.
        relations: Optional relation names to keep.
    """

    as_of: datetime | None = None
    known_at: datetime | None = None
    relations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked fact.

    Attributes:
        fact: The matching fact with both of its time intervals.
        text: Sentence the retrievers scored.
        score: Fused score, higher first.
    """

    fact: TemporalFact
    text: str
    score: float

    @property
    def reason(self) -> str:
        """Return the recipe that produced this hit."""

        return f"{'+'.join(RETRIEVERS)} then rrf"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Internal retrieval candidate before fusion.

    Attributes:
        fact: The fact this candidate stands for.
        text: Sentence the retrievers score.
        tokens: Tokens of the fact sentence and its resolved triplet.
        order: Stable tie-break key. Ranking must never fall back on
            `MemoryBlock.id`, which is a fresh UUID on every run: an id-ordered
            tie changes a fact's rank inside a retriever, which changes its
            reciprocal-rank contribution and makes scores differ run to run.
    """

    fact: TemporalFact
    text: str
    tokens: frozenset[str]
    order: str


class GraphitiSearch:
    """Hybrid fact search over one temporal knowledge graph."""

    def __init__(self, graph: TemporalKnowledgeGraph) -> None:
        """Create a search facade over a graph.

        Args:
            graph: Graph whose facts are searched.
        """

        self._graph = graph

    def search(
        self,
        query: str,
        *,
        center: str | Entity | None = None,
        filters: SearchFilters | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Run the hybrid recipe and return fused hits.

        Args:
            query: Natural-language query text.
            center: Optional entity that anchors graph traversal. Without one
                the `graph` retriever contributes nothing.
            filters: Optional bi-temporal and relation restrictions.
            limit: Maximum hits to return.

        Returns:
            Hits ordered by fused score, highest first.

        Raises:
            ValueError: If the query is empty or `limit` is negative.
        """

        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search query cannot be empty.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("Search limit must be a non-negative integer.")

        candidates = self._candidates(filters if filters is not None else SearchFilters())
        anchor = self._graph.entity_of(center) if center is not None else None
        tokens = _tokenize(query)
        fused = _reciprocal_rank_fusion(
            (
                self._keyword_ranking(query, candidates),
                _similarity_ranking(tokens, candidates),
                self._graph_ranking(candidates, anchor=anchor),
            )
        )
        ordered = sorted(
            fused.items(),
            key=lambda item: (-item[1], candidates[item[0]].order),
        )
        return [
            SearchHit(
                fact=candidates[fact_id].fact,
                text=candidates[fact_id].text,
                score=round(score, 6),
            )
            for fact_id, score in ordered[:limit]
        ]

    def _candidates(self, filters: SearchFilters) -> dict[str, _Candidate]:
        """Narrow the fact set with the bi-temporal and relation filters."""

        if filters.as_of is not None:
            facts = self._graph.facts_at(filters.as_of, known_at=filters.known_at)
        elif filters.known_at is not None:
            facts = self._graph.facts_known_at(filters.known_at)
        else:
            facts = self._graph.facts()
        return {
            fact.id: _Candidate(
                fact=fact,
                text=fact.fact,
                tokens=_tokenize(f"{fact.fact} {fact.statement}"),
                order=f"{fact.statement}|{fact.valid_at.isoformat()}",
            )
            for fact in facts
            if not filters.relations or fact.relation in filters.relations
        }

    def _keyword_ranking(self, query: str, candidates: dict[str, _Candidate]) -> list[str]:
        """Rank candidates by how many query terms Genie search matched.

        One `MemoryHandle.search` per term is a transparent stand-in for BM25:
        the portable query layer does the matching, and the lamp only counts
        which candidates each term reached.
        """

        matches: dict[str, int] = {}
        for term in sorted(_tokenize(query)):
            for result in self._graph.memory.search(
                text=term,
                types=[FACT_TYPE],
                limit=SEARCH_LIMIT,
            ):
                if result.block.id in candidates:
                    matches[result.block.id] = matches.get(result.block.id, 0) + 1
        return sorted(
            matches,
            key=lambda fact_id: (-matches[fact_id], candidates[fact_id].order),
        )

    def _graph_ranking(
        self,
        candidates: dict[str, _Candidate],
        *,
        anchor: Entity | None,
    ) -> list[str]:
        """Rank candidates by breadth-first distance from a center entity."""

        if anchor is None:
            return []
        distances: dict[str, int] = {}
        for path in self._graph.structures.graph.walk(
            anchor.block,
            direction="both",
            max_depth=_MAX_GRAPH_DEPTH,
            limit=SEARCH_LIMIT,
        ):
            for depth, block_id in enumerate(path.block_ids):
                if block_id not in distances or depth < distances[block_id]:
                    distances[block_id] = depth
        reachable = [fact_id for fact_id in candidates if fact_id in distances]
        return sorted(
            reachable,
            key=lambda fact_id: (distances[fact_id], candidates[fact_id].order),
        )


def _reciprocal_rank_fusion(rankings: Sequence[Sequence[str]]) -> dict[str, float]:
    """Fuse ranked id lists with reciprocal rank fusion.

    Args:
        rankings: One ranked id list per retriever.

    Returns:
        Fused score per id that appeared in at least one ranking.
    """

    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, fact_id in enumerate(ranking, start=1):
            fused[fact_id] = fused.get(fact_id, 0.0) + 1.0 / (_RRF_CONSTANT + rank)
    return fused


def _similarity_ranking(tokens: frozenset[str], candidates: dict[str, _Candidate]) -> list[str]:
    """Rank candidates by token-set cosine overlap with the query."""

    matched = [
        (fact_id, score)
        for fact_id, candidate in candidates.items()
        if (score := _cosine(tokens, candidate.tokens)) > 0.0
    ]
    return [
        fact_id
        for fact_id, _ in sorted(
            matched,
            key=lambda item: (-item[1], candidates[item[0]].order),
        )
    ]


def _cosine(left: frozenset[str], right: frozenset[str]) -> float:
    """Return cosine similarity between two token sets."""

    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _tokenize(value: str) -> frozenset[str]:
    """Split text into lowercase alphanumeric tokens."""

    return frozenset(_TOKEN_PATTERN.findall(value.casefold()))
