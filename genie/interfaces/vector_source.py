"""Abstract contract for searchable vector and text storage backends."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class IVectorSource(ABC):
    """Defines the interface for indexing and retrieving searchable content."""

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
        """Create or update a searchable document representation for a node.

        Parameters:
        node_id:
            The node identifier associated with the searchable record.
        raw_text:
            The text to index for lexical retrieval.
        dense_vector:
            The embedding vector to index for semantic retrieval.
        path_string:
            The absolute path used for scoped filtering.
        record_status:
            The lifecycle status that controls search visibility.
        last_error:
            Optional diagnostic message associated with the current status.

        Returns:
        bool
            ``True`` when the document was written successfully.
        """
        pass

    @abstractmethod
    def hybrid_search(self, query_text: str, query_vector: List[float], path_filter: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Run a hybrid lexical and vector search against stored documents.

        Parameters:
        query_text:
            The natural language query for lexical retrieval.
        query_vector:
            The embedding vector used for semantic retrieval.
        path_filter:
            The absolute path prefix used to scope results.
        top_k:
            The maximum number of fused results to return.

        Returns:
        list[dict[str, Any]]
            Ranked search results containing node metadata and scores.
        """
        pass
