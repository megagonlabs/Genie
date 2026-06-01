"""Storage Interface Definitions.

This module defines the abstract contract for all database and memory operations.
By strictly typing the inputs and outputs here, the rest of the framework (like
the ContextProvider and TaskFlowManager) remains completely agnostic to the actual
database being used.

A developer can hot-swap the local JSON file for a production Redis, PostgreSQL,
or Pinecone cluster simply by creating a new class that inherits from this interface.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Any
from models.memory import MemoryItem, MemoryScope


class BaseMemoryStorage(ABC):
    """The Abstract Base Class for memory persistence and RAG retrieval.

    Any custom database integration built for this framework MUST inherit from
    this class and implement all of the abstract methods below.
    """

    @abstractmethod
    def save(self, memory: MemoryItem) -> None:
        """Saves a newly instantiated MemoryItem to the database.

        Implementation Note for Developers:
        If your database engine does not automatically calculate vector embeddings,
        this method should intercept the MemoryItem, calculate the embeddings
        using an embedding model, append them to `memory.embeddings`, and THEN
        save it to the database.

        Parameters:
            memory: The populated MemoryItem object to persist.
        """
        pass

    @abstractmethod
    def get(self, memory_id: str) -> Optional[MemoryItem]:
        """Retrieves a specific MemoryItem using its exact ID.

        This is a traditional point-lookup (not a semantic search). It is
        primarily used by the TaskFlowManager to quickly fetch the mechanical
        TaskState JSON.

        Parameters:
            memory_id: The exact UUID string of the memory.

        Returns:
            The hydrated MemoryItem if found, otherwise None.
        """
        pass

    @abstractmethod
    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:
        """Performs a semantic/lexical search to retrieve relevant context.

        Implementation Note for Developers:
        A robust implementation should utilize Hybrid Search (Vectors + BM25).
        It MUST strictly honor the `scope`, `session_id`, and `user_id` arguments
        to prevent cross-contamination of isolated data between concurrent users.

        Parameters:
            query: The natural language string the agent is looking for.
            scope: The domain boundary (SESSION, USER, or GLOBAL) to filter by.
            limit: The maximum number of documents to return (default 5).
            session_id: Hard filter. If provided, MUST only return memories tagged with this ID.
            user_id: Hard filter. If provided, MUST only return memories tagged with this user.

        Returns:
            A list of ranked MemoryItems matching the search criteria.
        """
        pass

    @abstractmethod
    def update(self, memory_id: str, new_content: Any) -> None:
        """Updates the content of an existing MemoryItem in place.

        Implementation Note for Developers:
        If your implementation manages vector embeddings locally, you MUST clear
        the old embeddings and recalculate them based on the `new_content`
        before saving the update.

        Parameters:
            memory_id: The UUID of the record to overwrite.
            new_content: The updated data payload (string, dict, etc.).
        """
        pass

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """Permanently removes a specific MemoryItem from the database.

        Parameters:
            memory_id: The exact UUID string of the memory to delete.
        """
        pass
