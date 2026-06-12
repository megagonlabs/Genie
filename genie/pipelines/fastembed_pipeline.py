from typing import List, Optional
from fastembed import TextEmbedding
from genie.interfaces.embedder import IEmbedder


class FastEmbedPipeline(IEmbedder):
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", threads: int = 4):
        self.model = TextEmbedding(model_name=model_name, threads=threads)
        self._dimension: Optional[int] = None

    def generate_vector(self, text: str) -> List[float]:
        embeddings_list = list(self.model.embed([text]))
        first_embedding = embeddings_list[0]
        vector = first_embedding.tolist()
        if self._dimension is None:
            self._dimension = len(vector)
        return vector

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self.generate_vector("dimension probe")
        if self._dimension is None:
            raise RuntimeError("Failed to determine embedding dimension.")
        return self._dimension
