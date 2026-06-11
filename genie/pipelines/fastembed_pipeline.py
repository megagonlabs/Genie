from typing import List
from fastembed import TextEmbedding
from genie.interfaces.embedder import IEmbedder


class FastEmbedPipeline(IEmbedder):
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", threads: int = 4):
        self.model = TextEmbedding(model_name=model_name, threads=threads)

    def generate_vector(self, text: str) -> List[float]:
        embeddings_list = list(self.model.embed([text]))
        first_embedding = embeddings_list[0]
        return first_embedding.tolist()
