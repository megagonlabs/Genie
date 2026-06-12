"""FastEmbed-backed embedding pipeline used by Genie."""

from typing import List, Optional
from fastembed import TextEmbedding
from genie.interfaces.embedder import IEmbedder


class FastEmbedPipeline(IEmbedder):
    """Generate embeddings with the FastEmbed library.

    Parameters:
    model_name:
        The embedding model identifier passed to FastEmbed.
    threads:
        The number of worker threads FastEmbed may use.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", threads: int = 4):
        self.model = TextEmbedding(model_name=model_name, threads=threads)
        self._dimension: Optional[int] = None

    def generate_vector(self, text: str) -> List[float]:
        """Embed a piece of text into a dense vector.

        Parameters:
        text:
            The text to embed.

        Returns:
        list[float]
            The embedding vector produced by FastEmbed.
        """
        embeddings_list = list(self.model.embed([text]))
        first_embedding = embeddings_list[0]
        vector = first_embedding.tolist()
        if self._dimension is None:
            self._dimension = len(vector)
        return vector

    @property
    def dimension(self) -> int:
        """Return the embedding dimension produced by the configured model."""
        if self._dimension is None:
            self.generate_vector("dimension probe")
        if self._dimension is None:
            raise RuntimeError("Failed to determine embedding dimension.")
        return self._dimension
