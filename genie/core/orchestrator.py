import uuid
from typing import Optional, List, Dict, Any
from genie.exceptions import ConfigurationError
from genie.interfaces.index_manager import IIndexManager
from genie.interfaces.json_source import IJsonSource
from genie.interfaces.vector_source import IVectorSource
from genie.interfaces.embedder import IEmbedder
from genie.models.node import Node


class MemoryOrchestrator:
    def __init__(self, json_source: IJsonSource, vector_source: IVectorSource, index_manager: IIndexManager, embedder: Optional[IEmbedder] = None):  # <-- 2. Type hint the Interface
        self._json_source = json_source
        self._vector_source = vector_source
        self._index_manager = index_manager
        self._embedder = embedder or self._build_default_embedder()
        self._is_setup = False
        self._observed_vector_dimension: Optional[int] = None

    def setup(self):
        self._index_manager.initialize_index()
        self._is_setup = True

    def create_workspace(self, name: str, parent_id: Optional[str] = None) -> Node:
        self._validate_non_empty(name, "name")
        self._ensure_existing_node(parent_id, "parent_id")
        new_node_id = f"workspace_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=parent_id)
        self._json_source.upsert_node(node, record_status="ready", last_error="")
        return node

    def save_memory(self, workspace_node_id: str, content_text: str, name: str = "memory_node") -> Optional[Node]:
        self._validate_non_empty(name, "name")
        self._ensure_existing_node(workspace_node_id, "workspace_node_id")
        new_node_id = f"node_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=workspace_node_id, payload={"text": content_text})
        node_inserted = False
        try:
            self._json_source.upsert_node(node, record_status="pending", last_error="")
            node_inserted = True
            if content_text.strip():
                self._ensure_ready()
                vector = self._embedder.generate_vector(content_text)
                self._validate_vector_dimension(vector)
                path_string = self._json_source.get_absolute_path(new_node_id)
                self._vector_source.upsert_document(
                    node_id=new_node_id,
                    raw_text=content_text,
                    dense_vector=vector,
                    path_string=path_string,
                    record_status="ready",
                    last_error="",
                )
            else:
                self._json_source.set_record_status(new_node_id, "ready", "")
            return node
        except Exception as exc:
            try:
                if node_inserted and self._json_source.get_node(new_node_id) is not None:
                    self._json_source.set_record_status(new_node_id, "failed", str(exc))
            except Exception:
                pass
            raise

    def search_memory(self, query_text: str, scope_node_id: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
        self._validate_non_empty(query_text, "query_text")
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0.")
        if scope_node_id:
            self._ensure_existing_node(scope_node_id, "scope_node_id")
            path_filter = self._json_source.get_absolute_path(scope_node_id)
        else:
            path_filter = "/"
        self._ensure_ready()
        query_vector = self._embedder.generate_vector(query_text)
        self._validate_vector_dimension(query_vector)
        return self._vector_source.hybrid_search(query_text=query_text, query_vector=query_vector, path_filter=path_filter, top_k=top_k)

    def get_memory(self, memory_node_id: str) -> Optional[Node]:
        return self._json_source.get_node(memory_node_id)

    def delete_memory(self, memory_node_id: str) -> bool:
        return self._json_source.delete_node(memory_node_id)

    def _ensure_existing_node(self, node_id: Optional[str], field_name: str) -> None:
        if not node_id:
            return
        if self._json_source.get_node(node_id) is None:
            raise ValueError(f"{field_name} '{node_id}' does not exist. Use an exact node_id returned by Genie, not a display name.")

    def _ensure_ready(self) -> None:
        if not self._is_setup:
            self.setup()

    def _validate_non_empty(self, value: str, field_name: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{field_name} must not be empty.")

    def _validate_vector_dimension(self, vector: List[float]) -> None:
        current_dimension = len(vector)
        if current_dimension == 0:
            raise ConfigurationError("Embedder returned an empty vector.")
        if self._observed_vector_dimension is None:
            self._observed_vector_dimension = current_dimension
        elif current_dimension != self._observed_vector_dimension:
            raise ConfigurationError(
                f"Embedder returned inconsistent vector dimensions: expected {self._observed_vector_dimension}, got {current_dimension}."
            )

        expected_dimension = getattr(self._index_manager, "vector_dimension", None)
        if isinstance(expected_dimension, int) and current_dimension != expected_dimension:
            raise ConfigurationError(
                f"Embedder vector dimension {current_dimension} does not match index dimension {expected_dimension}."
            )

    def _build_default_embedder(self) -> IEmbedder:
        from genie.pipelines.fastembed_pipeline import FastEmbedPipeline

        return FastEmbedPipeline()
