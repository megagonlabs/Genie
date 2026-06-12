import numpy as np
import re
from typing import List, Dict, Any, Optional
from redis import Redis
from redis.commands.search.query import Query
from genie.interfaces.vector_source import IVectorSource


class RedisVectorSource(IVectorSource):
    def __init__(self, redis_client: Redis, index_name: str = "genie_idx"):
        self.client = redis_client
        self.index_name = index_name

    def _escape_tag(self, tag_value: str) -> str:
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
        vector_bytes = np.array(dense_vector, dtype=np.float32).tobytes()
        redis_key = f"node:{node_id}"
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
        candidate_count = max(top_k * 2, 10)
        text_results = self._run_text_search(query_text=query_text, path_filter=path_filter, limit=candidate_count)
        vector_results = self._run_vector_search(
            query_vector=query_vector, path_filter=path_filter, limit=candidate_count
        )
        return self._fuse_rankings(text_results=text_results, vector_results=vector_results, top_k=top_k)

    def _run_text_search(self, query_text: str, path_filter: str, limit: int) -> List[Any]:
        query_string = self._build_text_query(query_text=query_text, path_filter=path_filter)
        query_obj = Query(query_string).return_fields("search_text").paging(0, limit).dialect(2)
        search_results: Any = self.client.ft(self.index_name).search(query_obj)
        return list(search_results.docs)

    def _run_vector_search(self, query_vector: List[float], path_filter: str, limit: int) -> List[Any]:
        vector_bytes = np.array(query_vector, dtype=np.float32).tobytes()
        query_string = self._build_vector_query(path_filter=path_filter, limit=limit)
        query_obj = Query(query_string).return_fields("vector_score", "search_text").sort_by("vector_score").paging(0, limit).dialect(2)
        search_results: Any = self.client.ft(self.index_name).search(query_obj, query_params={"query_vec": vector_bytes})
        return list(search_results.docs)

    def _build_text_query(self, query_text: str, path_filter: str) -> str:
        text_terms = self._normalize_text_query(query_text)
        filters = self._combine_filters(path_filter)
        if filters and text_terms:
            return f"({filters} {text_terms})"
        if filters:
            return filters
        return text_terms or "@record_status:{ready}"

    def _build_vector_query(self, path_filter: str, limit: int) -> str:
        filters = self._combine_filters(path_filter)
        if filters:
            return f"({filters})=>[KNN {limit} @embedding $query_vec AS vector_score]"
        return f"(@record_status:{{ready}})=>[KNN {limit} @embedding $query_vec AS vector_score]"

    def _build_path_clause(self, path_filter: str) -> str:
        if path_filter in ["/", "", "*"]:
            return ""
        escaped_filter = self._escape_tag(path_filter)
        return f"@path:{{{escaped_filter}*}}"

    def _combine_filters(self, path_filter: str) -> str:
        path_clause = self._build_path_clause(path_filter)
        status_clause = "@record_status:{ready}"
        if path_clause:
            return f"{path_clause} {status_clause}"
        return status_clause

    def _normalize_text_query(self, query_text: str) -> str:
        terms = [self._escape_text_term(term) for term in re.findall(r"\w+", query_text)]
        return " ".join(term for term in terms if term)

    def _escape_text_term(self, term: str) -> str:
        return re.sub(r'([\\@{}\[\]\(\)"\':;!~\-])', r"\\\1", term)

    def _fuse_rankings(self, text_results: List[Any], vector_results: List[Any], top_k: int) -> List[Dict[str, Any]]:
        fused_scores: Dict[str, float] = {}
        texts: Dict[str, str] = {}
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
        return document_id.split(":", 1)[-1]
