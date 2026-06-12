from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class IVectorSource(ABC):

    @abstractmethod
    def upsert_document(
        self,
        node_id: str,
        raw_text: str,
        dense_vector: List[float],
        path_string: str,
        record_status: str = "ready",
        last_error: Optional[str] = "",
    ) -> bool:
        pass

    @abstractmethod
    def hybrid_search(self, query_text: str, query_vector: List[float], path_filter: str, top_k: int = 5) -> List[Dict[str, Any]]:
        pass
