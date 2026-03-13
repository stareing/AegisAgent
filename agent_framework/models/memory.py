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


class MemoryCandidateSource(str, Enum):
    """Origin of a memory candidate — determines write priority."""

    EXPLICIT_USER = "EXPLICIT_USER"  # User explicitly stated (highest trust)
    INFERRED = "INFERRED"            # Model-inferred from conversation
    TOOL_DERIVED = "TOOL_DERIVED"    # Extracted from tool output
    ADMIN = "ADMIN"                  # Administrative override


class MemoryConfidence(str, Enum):
    """Confidence level for a memory candidate."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MemoryCandidate(BaseModel):
    """A candidate memory to be evaluated for saving.

    Write priority rules:
    - EXPLICIT_USER + HIGH → always write (user said it directly)
    - INFERRED + LOW → conservative, ignore by default
    - TOOL_DERIVED → only write when structured, unambiguous, low-conflict
    """

    kind: MemoryKind = MemoryKind.CUSTOM
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    reason: str | None = None
    candidate_source: MemoryCandidateSource = MemoryCandidateSource.INFERRED
    confidence: MemoryConfidence = MemoryConfidence.MEDIUM


class MemorySourceContext(BaseModel):
    """Provenance metadata for a memory write operation.

    Tracks WHO wrote a memory so audit/governance can distinguish
    user-explicit saves from auto-extraction or sub-agent writes.
    """

    source_type: str = "agent"  # "user" | "agent" | "subagent" | "admin"
    source_run_id: str = ""
    source_spawn_id: str | None = None


class MemoryUpdateAction(str, Enum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"
    IGNORE = "IGNORE"


class CommitDecision(BaseModel):
    """Decision on whether to commit memory from a turn (v2.6.3 §41).

    Returned by record_turn(). Makes the commit/skip decision explicit
    and auditable instead of implicit.
    """

    committed: bool = False
    reason: str = ""
    source: str = "memory_manager"


class RunSessionOutcome(BaseModel):
    """Structured outcome passed to end_run_session() (v2.6.3 §41).

    Describes how the run terminated so the memory manager can decide
    cleanup behavior based on structured data, not ad-hoc conditionals.
    """

    status: str = "completed"  # "completed" | "aborted" | "cancelled"
    termination_kind: str = "NORMAL"  # matches TerminationKind values
    termination_reason: str = ""
    audit_ref: str = ""
