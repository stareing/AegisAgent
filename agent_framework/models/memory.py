from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class MemoryKind(str, Enum):
    USER_PROFILE = "USER_PROFILE"
    USER_PREFERENCE = "USER_PREFERENCE"
    USER_CONSTRAINT = "USER_CONSTRAINT"
    PROJECT_CONTEXT = "PROJECT_CONTEXT"
    TASK_HINT = "TASK_HINT"
    CUSTOM = "CUSTOM"


class MemoryRecord(BaseModel):
    """A single saved memory entry."""

    memory_id: str = ""
    user_id: str | None = None
    agent_id: str = ""
    kind: MemoryKind = MemoryKind.CUSTOM
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    is_active: bool = True
    is_pinned: bool = False
    source: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    use_count: int = 0
    version: int = 1
    extra: dict | None = None


class MemoryCandidate(BaseModel):
    """A candidate memory to be evaluated for saving."""

    kind: MemoryKind = MemoryKind.CUSTOM
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    reason: str | None = None


class MemoryUpdateAction(str, Enum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"
    IGNORE = "IGNORE"
