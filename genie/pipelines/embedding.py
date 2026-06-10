from typing import List
from fastembed import TextEmbedding


class EmbeddingPipeline:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", threads: int = 4):
        """
        Initializes the local embedding model.
        `threads` controls ONNX runtime parallelization.
        """
        self.model = TextEmbedding(model_name=model_name, threads=threads)

    def generate_vector(self, text: str) -> List[float]:
        """
        Translates raw text into a searchable dense vector.
        """
        # Cast the Iterable to a list and extract the first item.
        embeddings_list = list(self.model.embed([text]))
        first_embedding = embeddings_list[0]

        return first_embedding.tolist()
