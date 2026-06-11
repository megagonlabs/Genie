import numpy as np
import re
from typing import List, Dict, Any
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

    def upsert_document(self, node_id: str, raw_text: str, dense_vector: List[float], path_string: str) -> bool:
        vector_bytes = np.array(dense_vector, dtype=np.float32).tobytes()
        redis_key = f"node:{node_id}"
        mapping = {"search_text": raw_text, "embedding": vector_bytes, "path": path_string}
        self.client.hset(redis_key, mapping=mapping)
        return True

    def hybrid_search(self, query_text: str, query_vector: List[float], path_filter: str, top_k: int = 5) -> List[Dict[str, Any]]:
        vector_bytes = np.array(query_vector, dtype=np.float32).tobytes()
        if path_filter in ["/", "", "*"]:
            base_query_string = f"(*)=>[KNN {top_k} @embedding $query_vec AS vector_score]"
        else:
            escaped_filter = self._escape_tag(path_filter)
            base_query_string = f"(@path:{{{escaped_filter}*}})=>[KNN {top_k} @embedding $query_vec AS vector_score]"
        query_obj = Query(base_query_string).return_fields("vector_score", "search_text").sort_by("vector_score").dialect(2)
        search_results: Any = self.client.ft(self.index_name).search(query_obj, query_params={"query_vec": vector_bytes})
        parsed_results = []
        for doc in search_results.docs:
            actual_node_id = doc.id.split(":", 1)[-1]
            parsed_results.append({"node_id": actual_node_id, "score": float(getattr(doc, "vector_score", 0.0)), "text": getattr(doc, "search_text", "")})
        return parsed_results
