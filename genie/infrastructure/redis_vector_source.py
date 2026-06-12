"""Redis-backed vector and lexical search storage for Genie."""

import numpy as np
import re
from typing import List, Dict, Any, Optional
from redis import Redis
from redis.commands.search.query import Query
from genie.interfaces.vector_source import IVectorSource


class RedisVectorSource(IVectorSource):
    """Store searchable content in Redis hashes and query it via RediSearch.

    Parameters:
    redis_client:
        Connected Redis client.
    index_name:
        RediSearch index name to query.
    key_prefix:
        Prefix used when constructing Redis hash keys for node documents.
    """

    def __init__(self, redis_client: Redis, index_name: str = "genie_idx", key_prefix: str = "node:"):
        self.client = redis_client
        self.index_name = index_name
        self.key_prefix = key_prefix

    def _node_key(self, node_id: str) -> str:
        """Return the Redis hash key for a document node."""
        return f"{self.key_prefix}{node_id}"

    def _escape_tag(self, tag_value: str) -> str:
        """Escape RediSearch tag query syntax characters in a path filter."""
        def escape_symbol(match):
            value = match.group(0)
            return f"\\{value}"

        return re.sub(r"[,.<>{}\[\]\"':;!@#$%^&*()\-+=~/| ]", escape_symbol, tag_value)

    def upsert_document(
        self,
        node_id: str,
        raw_text: str,
        dense_vector: List[float],
        path_string: str,
        record_status: str = "ready",
        last_error: Optional[str] = "",
    ) -> bool:
        """Write the searchable representation of a node into Redis.

        Parameters:
        node_id:
            Node identifier associated with the document.
        raw_text:
            Text to index for lexical retrieval.
        dense_vector:
            Embedding vector to index for semantic retrieval.
        path_string:
            Absolute path used for scoped filtering.
        record_status:
            Lifecycle status controlling search visibility.
        last_error:
            Optional diagnostic message for failed indexing states.

        Returns:
        bool
            ``True`` when the hash update succeeds.
        """
        vector_bytes = np.array(dense_vector, dtype=np.float32).tobytes()
        redis_key = self._node_key(node_id)
        mapping = {
            "search_text": raw_text,
            "embedding": vector_bytes,
            "path": path_string,
            "record_status": record_status,
            "last_error": last_error or "",
        }
        self.client.hset(redis_key, mapping=mapping)
        return True

    def hybrid_search(self, query_text: str, query_vector: List[float], path_filter: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Run BM25 + vector retrieval and fuse the rankings with RRF."""
        # Pull a slightly larger candidate set from each retrieval mode before fusion.
        candidate_count = max(top_k * 2, 10)
        text_results = self._run_text_search(query_text=query_text, path_filter=path_filter, limit=candidate_count)
        vector_results = self._run_vector_search(
            query_vector=query_vector, path_filter=path_filter, limit=candidate_count
        )
        return self._fuse_rankings(text_results=text_results, vector_results=vector_results, top_k=top_k)

    def _run_text_search(self, query_text: str, path_filter: str, limit: int) -> List[Any]:
        """Execute the lexical half of the hybrid search."""
        query_string = self._build_text_query(query_text=query_text, path_filter=path_filter)
        query_obj = Query(query_string).return_fields("search_text").paging(0, limit).dialect(2)
        search_results: Any = self.client.ft(self.index_name).search(query_obj)
        return list(search_results.docs)

    def _run_vector_search(self, query_vector: List[float], path_filter: str, limit: int) -> List[Any]:
        """Execute the vector KNN half of the hybrid search."""
        vector_bytes = np.array(query_vector, dtype=np.float32).tobytes()
        query_string = self._build_vector_query(path_filter=path_filter, limit=limit)
        query_obj = Query(query_string).return_fields("vector_score", "search_text").sort_by("vector_score").paging(0, limit).dialect(2)
        search_results: Any = self.client.ft(self.index_name).search(query_obj, query_params={"query_vec": vector_bytes})
        return list(search_results.docs)

    def _build_text_query(self, query_text: str, path_filter: str) -> str:
        """Build the RediSearch query used for lexical retrieval."""
        text_terms = self._normalize_text_query(query_text)
        filters = self._combine_filters(path_filter)
        if filters and text_terms:
            return f"({filters} {text_terms})"
        if filters:
            return filters
        return text_terms or "@record_status:{ready}"

    def _build_vector_query(self, path_filter: str, limit: int) -> str:
        """Build the RediSearch KNN query used for vector retrieval."""
        filters = self._combine_filters(path_filter)
        if filters:
            return f"({filters})=>[KNN {limit} @embedding $query_vec AS vector_score]"
        return f"(@record_status:{{ready}})=>[KNN {limit} @embedding $query_vec AS vector_score]"

    def _build_path_clause(self, path_filter: str) -> str:
        """Build the path filter clause for a scoped RediSearch query."""
        if path_filter in ["/", "", "*"]:
            return ""
        escaped_filter = self._escape_tag(path_filter)
        return f"@path:{{{escaped_filter}*}}"

    def _combine_filters(self, path_filter: str) -> str:
        """Combine the path and lifecycle filters for search queries."""
        path_clause = self._build_path_clause(path_filter)
        status_clause = "@record_status:{ready}"
        if path_clause:
            return f"{path_clause} {status_clause}"
        return status_clause

    def _normalize_text_query(self, query_text: str) -> str:
        """Tokenize and escape user query text for RediSearch full-text search."""
        terms = [self._escape_text_term(term) for term in re.findall(r"\w+", query_text)]
        return " ".join(term for term in terms if term)

    def _escape_text_term(self, term: str) -> str:
        """Escape special RediSearch text query characters in a token."""
        return re.sub(r'([\\@{}\[\]\(\)"\':;!~\-])', r"\\\1", term)

    def _fuse_rankings(self, text_results: List[Any], vector_results: List[Any], top_k: int) -> List[Dict[str, Any]]:
        """Fuse lexical and vector rankings with Reciprocal Rank Fusion."""
        fused_scores: Dict[str, float] = {}
        texts: Dict[str, str] = {}
        # A standard RRF constant that dampens the impact of very low ranks.
        rrf_k = 60

        for rank, doc in enumerate(text_results, start=1):
            node_id = self._extract_node_id(doc.id)
            fused_scores[node_id] = fused_scores.get(node_id, 0.0) + (1.0 / (rrf_k + rank))
            texts.setdefault(node_id, getattr(doc, "search_text", ""))

        for rank, doc in enumerate(vector_results, start=1):
            node_id = self._extract_node_id(doc.id)
            fused_scores[node_id] = fused_scores.get(node_id, 0.0) + (1.0 / (rrf_k + rank))
            texts.setdefault(node_id, getattr(doc, "search_text", ""))

        ranked_node_ids = sorted(
            fused_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return [
            {"node_id": node_id, "score": score, "text": texts.get(node_id, "")}
            for node_id, score in ranked_node_ids[:top_k]
        ]

    def _extract_node_id(self, document_id: str) -> str:
        """Convert a Redis document key back into the original node identifier."""
        if document_id.startswith(self.key_prefix):
            return document_id[len(self.key_prefix):]
        return document_id.split(":", 1)[-1]
