"""Memory Schema Definitions.

This module provides the Pydantic data models for the agentic memory layer.
It defines the storage structures for multi-tiered RAG (Retrieval-Augmented Generation),
enforcing strict scope boundaries across global data, individual user configurations,
and isolated execution sessions.
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from enum import Enum
import uuid


class MemoryScope(str, Enum):
    """Defines the visibility and access boundary of a stored memory item."""

    GLOBAL = "global"
    """System-wide guidelines, static recipes, fallback instructions, or immutable facts."""

    USER = "user"
    """Long-term preferences, profile data, or constraints tied to a specific end-user."""

    SESSION = "session"
    """Short-term, highly ephemeral context isolated to a single active workflow run."""


class MemoryItem(BaseModel):
    """Represents a unified knowledge snippet or structural state inside the storage layer.

    This model acts as the data structure passed between the storage engines,
    the context providers, and the ranking algorithms.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="The globally unique identifier for this specific memory entry.")

    scope: MemoryScope = Field(description="The scope tier (GLOBAL, USER, or SESSION) that dictates visibility and multi-tenant isolation.")

    session_id: Optional[str] = Field(default=None, description="The parent task or workflow execution ID. Required if the scope is set to SESSION.")

    user_id: Optional[str] = Field(default=None, description="The unique identifier of the target end-user. Required if the scope is set to USER to ensure security.")

    content: Any = Field(description="The core data payload. Can be a text string for RAG knowledge, or a serialized dictionary representing a workflow state graph.")

    embeddings: Dict[str, List[float]] = Field(
        default_factory=dict, description="Maps an embedding model's name (e.g., 'fastembed-default') to its high-dimensional mathematical vector representation."
    )

    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary key-value store for telemetry, source tracking, filtering flags, or access metrics (like access_count).")

    created_at: float = Field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp(), description="The high-precision UTC Unix epoch timestamp recording exactly when this item was saved to disk."
    )
