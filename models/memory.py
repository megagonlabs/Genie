from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from enum import Enum
import uuid


class MemoryScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    SESSION = "session"


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    scope: MemoryScope
    session_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    content: Any
    embeddings: Dict[str, List[float]] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
