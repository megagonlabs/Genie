import uuid
import logging
from typing import Optional, List, Dict, Any
from genie.interfaces.index_manager import IIndexManager
from genie.interfaces.json_source import IJsonSource
from genie.interfaces.vector_source import IVectorSource
from genie.pipelines.embedding import EmbeddingPipeline
from genie.models.node import Node

logger = logging.getLogger(__name__)


class MemoryOrchestrator:
    def __init__(self, json_source: IJsonSource, vector_source: IVectorSource, index_manager: IIndexManager, embedder: Optional[EmbeddingPipeline] = None):
        self._json_source = json_source
        self._vector_source = vector_source
        self._index_manager = index_manager
        self._embedder = embedder or EmbeddingPipeline()

    def setup(self):
        """
        Explicitly initializes the underlying database schemas and indexes.
        Call this once during application startup or deployment.
        """
        logger.info("Initializing Memory Orchestrator infrastructure...")
        self._index_manager.initialize_index()
        logger.info("Infrastructure ready.")

    def create_workspace(self, name: str, parent_id: Optional[str] = None) -> Node:
        """
        Creates a structural folder or root workspace.
        Does not trigger the semantic vector index, saving compute and database space.
        """
        # Use full 32-character hex to mathematically eliminate ID collisions at scale
        new_node_id = f"workspace_{uuid.uuid4().hex}"

        node = Node(node_id=new_node_id, name=name, parent_id=parent_id)
        self._json_source.upsert_node(node)

        logger.info(f"Created workspace/folder {new_node_id}")
        return node

    def save_memory(self, workspace_node_id: str, content_text: str, name: str = "memory_node") -> Optional[Node]:
        """
        Creates and stores a memory node. Automatically coordinates both the
        structural JSON graph and the semantic vector index.
        """
        new_node_id = f"node_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=workspace_node_id, payload={"text": content_text})

        try:
            # 1. Structural Write
            self._json_source.upsert_node(node)

            # 2. Semantic Write (Only index if there is actually content)
            if content_text:
                vector = self._embedder.generate_vector(content_text)
                path_string = self._json_source.get_absolute_path(new_node_id)
                self._vector_source.upsert_document(node_id=new_node_id, raw_text=content_text, dense_vector=vector, path_string=path_string)

            logger.info(f"Successfully saved memory {new_node_id} to workspace {workspace_node_id}")
            return node

        except Exception as e:
            logger.error(f"Failed to save memory {new_node_id}, initiating rollback: {e}")
            self._json_source.delete_node(new_node_id)
            raise e

    def search_memory(self, query_text: str, scope_node_id: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Executes a localized hybrid search. If scope_node_id is None, it performs
        a global search across all workspaces.
        """
        # Gracefully handle global searches vs localized searches
        if scope_node_id:
            path_filter = self._json_source.get_absolute_path(scope_node_id)
        else:
            path_filter = "/"

        # Generate the dense mathematical vector from raw text
        query_vector = self._embedder.generate_vector(query_text)

        # Execute the underlying search completely out of sight of the developer
        return self._vector_source.hybrid_search(query_text=query_text, query_vector=query_vector, path_filter=path_filter, top_k=top_k)

    def get_memory(self, memory_node_id: str) -> Optional[Node]:
        """Fetches a specific memory node directly by its identifier."""
        return self._json_source.get_node(memory_node_id)

    def delete_memory(self, memory_node_id: str) -> bool:
        """
        Triggers an iterative cascading deletion of a memory or a cluster folder.
        Wipes out all corresponding sub-nodes from both structural and semantic stores.
        """
        logger.info(f"Initiating cascading deletion for node tree starting at: {memory_node_id}")
        return self._json_source.delete_node(memory_node_id)
