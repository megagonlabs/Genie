"""Hybrid fact retrieval for the Graphiti lamp.

- **Upstream:** Graphiti does not use one index for retrieval. It runs several
  retrievers over the graph and fuses their ranked results.
- **Default recipe:** `EDGE_HYBRID_SEARCH_RRF` combines full-text search,
  embedding similarity, and breadth-first graph traversal over facts. It fuses
  the results with reciprocal rank fusion. This module implements only this
  recipe.

Genie leaves ranking to applications. So the lamp uses the library only in the
retrievers and the filters:

- **`keyword`:** sends one `MemoryHandle.search` for each query term.
- **`graph`:** walks `PropertyGraph` outward from a center entity.
- **`similarity`:** uses the cosine overlap of token sets, without Genie. It is
  a visible replacement for embeddings.
- **Bi-temporal filters:** these are the other part that uses Genie. They narrow
  the candidate set through the same temporal collections in which the graph
  stores facts.

The lamp intentionally does not reproduce the other recipes of Graphiti, or its
MMR, node-distance, episode-mention, and cross-encoder rerankers. The lamp
README lists them as omissions.
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
    """Restrictions that apply before retrieval starts.

    - **`as_of`:** a bi-temporal filter. It keeps the facts that were true at
      that time.
    - **`known_at`:** a bi-temporal filter. It keeps the facts that the graph
      had on record at that time.

    Attributes:
        as_of: Optional effective time. Search keeps only facts that were true
            at this time.
        known_at: Optional ingestion time. Search keeps only facts that were on
            record at this time.
        relations: Optional relation names to keep. An empty tuple keeps all
            relations.
    """

    as_of: datetime | None = None
    known_at: datetime | None = None
    relations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked fact.

    Attributes:
        fact: The fact that matches, with both of its time intervals.
        text: Sentence that the retrievers scored.
        score: Fused score, rounded to 6 decimal places. A higher score ranks
            first.
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
        fact: The fact that this candidate represents.
        text: Sentence that the retrievers score.
        tokens: Tokens of the fact sentence and its resolved triplet.
        order: Stable tie-break key from the resolved triplet and the start of
            the valid time. Ranking never uses `MemoryBlock.id`, because the
            id is a new UUID on every run. A tie that the id breaks changes the
            rank of a fact inside a retriever. That changes its reciprocal-rank
            contribution, and the scores differ from run to run.
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
            graph: Graph with the facts to search.
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
        """Run the hybrid recipe and return the fused hits.

        Args:
            query: Natural-language query text.
            center: Optional entity at the start of the graph traversal.
                Without it, the `graph` retriever adds nothing.
            filters: Optional bi-temporal and relation restrictions.
            limit: Maximum number of hits to return.

        Returns:
            Hits in fused score order, highest first. Ties use the stable
            order key of each fact.

        Raises:
            ValueError: If the query is not a string or is empty, or if `limit`
                is not an integer, is a boolean, or is negative.
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
        """Rank candidates by the number of query terms that Genie search found.

        - **Replacement for BM25:** one `MemoryHandle.search` for each term is a
          transparent replacement for BM25.
        - **Division of work:** the portable query layer matches the terms. The
          lamp only counts the candidates that each term found.
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

    Each ranking adds `1 / (_RRF_CONSTANT + rank)` to the score of each id. The
    first rank is 1.

    Args:
        rankings: One ranked id list for each retriever.

    Returns:
        The fused score of each id that is in 1 or more rankings.
    """

    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, fact_id in enumerate(ranking, start=1):
            fused[fact_id] = fused.get(fact_id, 0.0) + 1.0 / (_RRF_CONSTANT + rank)
    return fused


def _similarity_ranking(tokens: frozenset[str], candidates: dict[str, _Candidate]) -> list[str]:
    """Rank candidates by the cosine overlap of their tokens with the query.

    The ranking leaves out candidates with no overlap.
    """

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
    """Return the cosine similarity between 2 token sets."""

    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _tokenize(value: str) -> frozenset[str]:
    """Split case-folded text into tokens of letters `a` to `z` and digits."""

    return frozenset(_TOKEN_PATTERN.findall(value.casefold()))
