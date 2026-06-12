"""High-level orchestration layer that coordinates Genie storage and search."""

import uuid
from typing import Optional, List, Dict, Any
from genie.exceptions import ConfigurationError
from genie.interfaces.index_manager import IIndexManager
from genie.interfaces.json_source import IJsonSource
from genie.interfaces.vector_source import IVectorSource
from genie.interfaces.embedder import IEmbedder
from genie.models.node import Node


class MemoryOrchestrator:
    """Coordinate node storage, indexing, and retrieval across Genie services.

    Parameters:
    json_source:
        Backend used for hierarchical node persistence.
    vector_source:
        Backend used for lexical and semantic search indexing.
    index_manager:
        Component responsible for index provisioning and runtime verification.
    embedder:
        Optional embedding backend. When omitted, Genie uses FastEmbed.
    """

    def __init__(self, json_source: IJsonSource, vector_source: IVectorSource, index_manager: IIndexManager, embedder: Optional[IEmbedder] = None):  # <-- 2. Type hint the Interface
        self._json_source = json_source
        self._vector_source = vector_source
        self._index_manager = index_manager
        self._embedder = embedder or self._build_default_embedder()
        self._is_setup = False
        self._observed_vector_dimension: Optional[int] = None

    def setup(self):
        """Verify that the configured search index already exists and matches.

        This method is intended for application startup in environments where
        index provisioning is handled separately from runtime request handling.
        """
        self._index_manager.verify_index()
        self._is_setup = True

    def provision(self) -> None:
        """Provision the configured index explicitly and mark the orchestrator ready.

        This is intended for development setups, one-time bootstrap flows, or
        controlled administrative tooling rather than hot request paths.
        """
        self._index_manager.create_index()
        self._is_setup = True

    def create_workspace(self, name: str, parent_id: Optional[str] = None) -> Node:
        """Create a workspace node in the hierarchy.

        Parameters:
        name:
            Human-readable workspace name.
        parent_id:
            Optional parent workspace identifier for nested structures.

        Returns:
        Node
            The newly created workspace node.
        """
        self._validate_non_empty(name, "name")
        self._ensure_existing_node(parent_id, "parent_id")
        new_node_id = f"workspace_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=parent_id)
        self._json_source.upsert_node(node, record_status="ready", last_error="")
        return node

    def save_memory(self, workspace_node_id: str, content_text: str, name: str = "memory_node") -> Optional[Node]:
        """Persist a memory node and optionally index it for search.

        Parameters:
        workspace_node_id:
            The workspace that should own the new memory.
        content_text:
            The text payload to store and, when non-empty, embed and index.
        name:
            Human-readable node name.

        Returns:
        Node | None
            The created memory node.
        """
        self._validate_non_empty(name, "name")
        self._ensure_existing_node(workspace_node_id, "workspace_node_id")
        new_node_id = f"node_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=workspace_node_id, payload={"text": content_text})
        node_inserted = False
        try:
            # Persist the node first so failed indexing can be tracked explicitly.
            self._json_source.upsert_node(node, record_status="pending", last_error="")
            node_inserted = True
            if content_text.strip():
                self._ensure_initialized()
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
                # Empty content is stored structurally but does not participate in search.
                self._json_source.set_record_status(new_node_id, "ready", "")
            return node
        except Exception as exc:
            try:
                # Preserve the node and failure reason so operators can inspect or retry it later.
                if node_inserted and self._json_source.get_node(new_node_id) is not None:
                    self._json_source.set_record_status(new_node_id, "failed", str(exc))
            except Exception:
                pass
            raise

    def search_memory(self, query_text: str, scope_node_id: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
        """Search indexed memories using fused lexical and semantic retrieval.

        Parameters:
        query_text:
            Natural language query text.
        scope_node_id:
            Optional workspace identifier used to scope results by path.
        top_k:
            Maximum number of search results to return.

        Returns:
        list[dict[str, Any]]
            Ranked search results from the vector source.
        """
        self._validate_non_empty(query_text, "query_text")
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0.")
        if scope_node_id:
            self._ensure_existing_node(scope_node_id, "scope_node_id")
            path_filter = self._json_source.get_absolute_path(scope_node_id)
        else:
            path_filter = "/"
        self._ensure_initialized()
        query_vector = self._embedder.generate_vector(query_text)
        self._validate_vector_dimension(query_vector)
        return self._vector_source.hybrid_search(query_text=query_text, query_vector=query_vector, path_filter=path_filter, top_k=top_k)

    def get_memory(self, memory_node_id: str) -> Optional[Node]:
        """Retrieve a stored memory node by identifier."""
        return self._json_source.get_node(memory_node_id)

    def delete_memory(self, memory_node_id: str) -> bool:
        """Delete a memory node and its subtree when present."""
        return self._json_source.delete_node(memory_node_id)

    def _ensure_existing_node(self, node_id: Optional[str], field_name: str) -> None:
        """Validate that an optional node identifier exists in storage."""
        if not node_id:
            return
        if self._json_source.get_node(node_id) is None:
            raise ValueError(f"{field_name} '{node_id}' does not exist. Use an exact node_id returned by Genie, not a display name.")

    def _ensure_initialized(self) -> None:
        """Ensure the orchestrator has been explicitly initialized."""
        if not self._is_setup:
            raise ConfigurationError("MemoryOrchestrator is not initialized. Call setup() to verify indexes or provision() to create them explicitly.")

    def _validate_non_empty(self, value: str, field_name: str) -> None:
        """Reject blank string inputs for required public API parameters."""
        if not value or not value.strip():
            raise ValueError(f"{field_name} must not be empty.")

    def _validate_vector_dimension(self, vector: List[float]) -> None:
        """Validate embedder output shape against the configured index schema.

        Parameters:
        vector:
            The embedding vector returned by the configured embedder.
        """
        current_dimension = len(vector)
        if current_dimension == 0:
            raise ConfigurationError("Embedder returned an empty vector.")
        if self._observed_vector_dimension is None:
            self._observed_vector_dimension = current_dimension
        elif current_dimension != self._observed_vector_dimension:
            raise ConfigurationError(f"Embedder returned inconsistent vector dimensions: expected {self._observed_vector_dimension}, got {current_dimension}.")

        expected_dimension = getattr(self._index_manager, "vector_dimension", None)
        if isinstance(expected_dimension, int) and current_dimension != expected_dimension:
            raise ConfigurationError(f"Embedder vector dimension {current_dimension} does not match index dimension {expected_dimension}.")

    def _build_default_embedder(self) -> IEmbedder:
        """Construct the default embedder lazily to avoid import-time coupling."""
        from genie.pipelines.fastembed_pipeline import FastEmbedPipeline

        return FastEmbedPipeline()
