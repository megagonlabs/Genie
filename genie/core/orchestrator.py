import uuid
from typing import Optional, List, Dict, Any
from genie.interfaces.index_manager import IIndexManager
from genie.interfaces.json_source import IJsonSource
from genie.interfaces.vector_source import IVectorSource
from genie.pipelines.fastembed_pipeline import FastEmbedPipeline
from genie.interfaces.embedder import IEmbedder
from genie.models.node import Node


class MemoryOrchestrator:
    def __init__(self, json_source: IJsonSource, vector_source: IVectorSource, index_manager: IIndexManager, embedder: Optional[IEmbedder] = None):  # <-- 2. Type hint the Interface
        self._json_source = json_source
        self._vector_source = vector_source
        self._index_manager = index_manager
        self._embedder = embedder or FastEmbedPipeline()

    def setup(self):
        self._index_manager.initialize_index()

    def create_workspace(self, name: str, parent_id: Optional[str] = None) -> Node:
        self._ensure_existing_node(parent_id, "parent_id")
        new_node_id = f"workspace_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=parent_id)
        self._json_source.upsert_node(node)
        return node

    def save_memory(self, workspace_node_id: str, content_text: str, name: str = "memory_node") -> Optional[Node]:
        self._ensure_existing_node(workspace_node_id, "workspace_node_id")
        new_node_id = f"node_{uuid.uuid4().hex}"
        node = Node(node_id=new_node_id, name=name, parent_id=workspace_node_id, payload={"text": content_text})
        try:
            self._json_source.upsert_node(node)
            if content_text:
                vector = self._embedder.generate_vector(content_text)
                path_string = self._json_source.get_absolute_path(new_node_id)
                self._vector_source.upsert_document(node_id=new_node_id, raw_text=content_text, dense_vector=vector, path_string=path_string)
            return node
        except Exception as e:
            self._json_source.delete_node(new_node_id)
            raise e

    def search_memory(self, query_text: str, scope_node_id: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
        if scope_node_id:
            self._ensure_existing_node(scope_node_id, "scope_node_id")
            path_filter = self._json_source.get_absolute_path(scope_node_id)
        else:
            path_filter = "/"
        query_vector = self._embedder.generate_vector(query_text)
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
