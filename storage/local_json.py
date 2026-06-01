import json
import os
from typing import List, Optional, Any, Dict
from storage.interface import BaseMemoryStorage
from models.memory import MemoryItem, MemoryScope
from storage.hybrid import bm25_lite_score, cosine_similarity, reciprocal_rank_fusion

try:
    from fastembed import TextEmbedding

    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


class LocalJSONStorage(BaseMemoryStorage):

    def __init__(self, file_path: str = "db.json"):
        self.file_path = file_path
        self._ensure_file()
        self.model_name = "fastembed-default"
        self.embedding_model = None
        if HAS_FASTEMBED:
            self.embedding_model = TextEmbedding()

    def _ensure_file(self) -> None:
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _read_all(self) -> List[Dict[str, Any]]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_all(self, data: List[Dict[str, Any]]) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def save(self, memory: MemoryItem) -> None:
        is_state_tracker = memory.metadata.get("type") == "task_operational_state"
        if self.embedding_model and self.model_name not in memory.embeddings and not is_state_tracker:
            text_to_embed = str(memory.content)
            embedding_array = list(self.embedding_model.embed([text_to_embed]))[0]
            memory.embeddings[self.model_name] = embedding_array.tolist()
        items = self._read_all()
        items.append(memory.model_dump(mode="json"))
        self._write_all(items)

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        items = self._read_all()
        for item in items:
            if item.get("id") == memory_id:
                return MemoryItem.model_validate(item)
        return None

    def update(self, memory_id: str, new_content: Any) -> None:
        items = self._read_all()
        for i, item in enumerate(items):
            if item.get("id") == memory_id:
                memory_obj = MemoryItem.model_validate(item)
                memory_obj.content = new_content
                memory_obj.embeddings = {}
                self.delete(memory_id)
                self.save(memory_obj)
                break

    def delete(self, memory_id: str) -> None:
        items = self._read_all()
        items = [item for item in items if item.get("id") != memory_id]
        self._write_all(items)

    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:  # <- NEW
        raw_items = self._read_all()
        corpus = []
        for item in raw_items:
            mem_item = MemoryItem.model_validate(item)
            if mem_item.scope == scope:
                if session_id and mem_item.session_id != session_id:
                    continue
                if user_id and mem_item.user_id != user_id:
                    continue
                corpus.append(mem_item)
        if not query or not corpus:
            return corpus[:limit]
        bm25_scores = bm25_lite_score(query, corpus)
        keyword_ranked = sorted([item for item in corpus if bm25_scores.get(item.id, 0) > 0], key=lambda x: bm25_scores[x.id], reverse=True)
        vector_ranked = []
        if self.embedding_model:
            query_vector = list(self.embedding_model.embed([query]))[0].tolist()
            vec_scores = {}
            for item in corpus:
                item_vec = item.embeddings.get(self.model_name)
                if item_vec:
                    vec_scores[item.id] = cosine_similarity(query_vector, item_vec)
            vector_ranked = sorted([item for item in corpus if vec_scores.get(item.id, 0) > 0], key=lambda x: vec_scores[x.id], reverse=True)
        if vector_ranked and keyword_ranked:
            final_results = reciprocal_rank_fusion([keyword_ranked, vector_ranked])
        elif vector_ranked:
            final_results = vector_ranked
        else:
            final_results = keyword_ranked
        top_results = final_results[:limit]
        if top_results:
            top_ids = {item.id for item in top_results}
            modified = False
            for raw_item in raw_items:
                if raw_item.get("id") in top_ids:
                    meta = raw_item.setdefault("metadata", {})
                    meta["access_count"] = meta.get("access_count", 0) + 1
                    modified = True
            if modified:
                self._write_all(raw_items)
            for item in top_results:
                item.metadata["access_count"] = item.metadata.get("access_count", 0) + 1
        return top_results
