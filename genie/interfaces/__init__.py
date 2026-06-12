"""Interface contracts used by Genie infrastructure and pipelines."""

from genie.interfaces.embedder import IEmbedder
from genie.interfaces.index_manager import IIndexManager
from genie.interfaces.json_source import IJsonSource
from genie.interfaces.vector_source import IVectorSource

__all__ = ["IEmbedder", "IIndexManager", "IJsonSource", "IVectorSource"]
