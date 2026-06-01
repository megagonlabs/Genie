"""Local JSON Storage Engine.

Implements the BaseMemoryStorage interface using a simple local JSON file.
Upgraded with FastEmbed for out-of-the-box local vector generation,
and full Hybrid Search (BM25 + Cosine Similarity + Reciprocal Rank Fusion).
"""

import json
import os
from typing import List, Optional, Any, Dict

from storages.interface import BaseMemoryStorage
from models.memory import MemoryItem, MemoryScope
from storages.hybrid import bm25_lite_score, cosine_similarity, reciprocal_rank_fusion

# Attempt to load the lightweight embedding library
try:
    from fastembed import TextEmbedding

    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


class LocalJSONStorage(BaseMemoryStorage):
    """Stores and retrieves memory items from a local JSON file.

    This acts as a fully functional, zero-external-dependency database
    perfect for ephemeral agentic workflows and local development.
    """

    def __init__(self, file_path: str = "db.json"):
        """Initializes the JSON storage engine and the local embedding model.

        Parameters:
            file_path: The relative or absolute path to the JSON database file.
        """
        self.file_path = file_path
        self._ensure_file()

        self.model_name = "fastembed-default"
        self.embedding_model = None

        # Initializes the local embedding model.
        # Downloads a lightweight (~120MB) model on the very first run, then caches it.
        if HAS_FASTEMBED:
            self.embedding_model = TextEmbedding()

    def _ensure_file(self) -> None:
        """Creates the JSON file with an empty list if it doesn't already exist."""
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _read_all(self) -> List[Dict[str, Any]]:
        """Helper to read the entire JSON array from disk."""
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_all(self, data: List[Dict[str, Any]]) -> None:
        """Helper to overwrite the JSON file with the provided array."""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def save(self, memory: MemoryItem) -> None:
        """Appends a new MemoryItem to the database and generates its vector embedding.

        Parameters:
            memory: The memory object to be saved.
        """
        # Guardrail: Do NOT embed mechanical state trackers. Only embed actual knowledge.
        is_state_tracker = memory.metadata.get("type") == "task_operational_state"

        # Auto-generate the vector embedding if FastEmbed is available
        if self.embedding_model and self.model_name not in memory.embeddings and not is_state_tracker:
            text_to_embed = str(memory.content)
            # FastEmbed returns a generator of numpy arrays; extract the first one
            embedding_array = list(self.embedding_model.embed([text_to_embed]))[0]
            # Convert numpy array to standard Python list for JSON serialization
            memory.embeddings[self.model_name] = embedding_array.tolist()

        items = self._read_all()
        items.append(memory.model_dump(mode="json"))
        self._write_all(items)

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        """Retrieves a specific MemoryItem by its ID."""
        items = self._read_all()
        for item in items:
            if item.get("id") == memory_id:
                return MemoryItem.model_validate(item)
        return None

    def update(self, memory_id: str, new_content: Any) -> None:
        """Updates the content of an existing MemoryItem and regenerates its embedding."""
        items = self._read_all()
        for i, item in enumerate(items):
            if item.get("id") == memory_id:
                memory_obj = MemoryItem.model_validate(item)
                memory_obj.content = new_content

                # Wipe old embeddings so they are forced to regenerate on save
                memory_obj.embeddings = {}

                # Delete the old item, save the new item to trigger embedding generation
                self.delete(memory_id)
                self.save(memory_obj)
                break

    def delete(self, memory_id: str) -> None:
        """Removes a MemoryItem from the JSON file."""
        items = self._read_all()
        items = [item for item in items if item.get("id") != memory_id]
        self._write_all(items)

    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:
        """Performs a full Hybrid Search with hard isolation and read observability.

        Parameters:
            query: The text to search for.
            scope: The memory scope boundary (e.g., SESSION or GLOBAL).
            limit: Maximum number of items to return.
            session_id: Hard filter to strictly isolate data to a specific run.
            user_id: Hard filter to strictly isolate data to a specific user.

        Returns:
            A ranked list of MemoryItems matching the search parameters.
        """
        raw_items = self._read_all()
        corpus = []

        # --- PRE-FILTERING: Hard filter by scope, session_id, AND user_id ---
        for item in raw_items:
            mem_item = MemoryItem.model_validate(item)
            if mem_item.scope == scope:
                # STRICT ISOLATION: Fail closed. If no ID is provided, or they don't match, block it.
                if scope == MemoryScope.SESSION and (not session_id or mem_item.session_id != session_id):
                    continue
                if scope == MemoryScope.USER and (not user_id or mem_item.user_id != user_id):
                    continue
                corpus.append(mem_item)

        if not query or not corpus:
            return corpus[:limit]

        # --- PILLAR 1: Lexical Search (BM25 Keyword Matching) ---
        bm25_scores = bm25_lite_score(query, corpus)
        keyword_ranked = sorted([item for item in corpus if bm25_scores.get(item.id, 0) > 0], key=lambda x: bm25_scores[x.id], reverse=True)

        # --- PILLAR 2: Semantic Search (Vector Cosine Similarity) ---
        vector_ranked = []
        if self.embedding_model:
            query_vector = list(self.embedding_model.embed([query]))[0].tolist()
            vec_scores = {}
            for item in corpus:
                item_vec = item.embeddings.get(self.model_name)
                if item_vec:
                    vec_scores[item.id] = cosine_similarity(query_vector, item_vec)
            vector_ranked = sorted([item for item in corpus if vec_scores.get(item.id, 0) > 0], key=lambda x: vec_scores[x.id], reverse=True)

        # --- PILLAR 3: Reciprocal Rank Fusion (Merging the lists) ---
        if vector_ranked and keyword_ranked:
            final_results = reciprocal_rank_fusion([keyword_ranked, vector_ranked])
        elif vector_ranked:
            final_results = vector_ranked
        else:
            final_results = keyword_ranked

        # Apply the final truncation limit
        top_results = final_results[:limit]

        # --- READ OBSERVABILITY (Data Lineage Tracking) ---
        # If results are returned, increment their 'access_count' so we have
        # an auditable trail of exactly which data the agent looked at.
        if top_results:
            top_ids = {item.id for item in top_results}
            modified = False

            # 1. Update the raw dictionaries to save back to disk
            for raw_item in raw_items:
                if raw_item.get("id") in top_ids:
                    meta = raw_item.setdefault("metadata", {})
                    meta["access_count"] = meta.get("access_count", 0) + 1
                    modified = True

            if modified:
                self._write_all(raw_items)

            # 2. Update the in-memory Python objects being returned to the ContextProvider
            for item in top_results:
                item.metadata["access_count"] = item.metadata.get("access_count", 0) + 1

        return top_results
