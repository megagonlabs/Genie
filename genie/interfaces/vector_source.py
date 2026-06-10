from abc import ABC, abstractmethod
from typing import List, Dict, Any


class IVectorSource(ABC):
    """Semantic & Keyword Search Contract"""

    @abstractmethod
    def upsert_document(self, node_id: str, raw_text: str, dense_vector: List[float], path_string: str) -> bool:
        """Stores text and vector mapped to node_id, tagged with path_string."""
        pass

    @abstractmethod
    def hybrid_search(self, query_text: str, query_vector: List[float], path_filter: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Executes BM25 + Vector search, filtered strictly by path_filter."""
        pass
