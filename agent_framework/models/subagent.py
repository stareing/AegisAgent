from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_framework.models.message import Message, TokenUsage


class SpawnMode(str, Enum):
    EPHEMERAL = "EPHEMERAL"
    FORK = "FORK"
    LONG_LIVED = "LONG_LIVED"


class MemoryScope(str, Enum):
    ISOLATED = "ISOLATED"
    INHERIT_READ = "INHERIT_READ"
    SHARED_WRITE = "SHARED_WRITE"


class SubAgentSpec(BaseModel):
    parent_run_id: str = ""
    spawn_id: str = ""
    mode: SpawnMode = SpawnMode.EPHEMERAL
    task_input: str = ""
    agent_config_override: dict = Field(default_factory=dict)
    skill_id: str | None = None
    tool_category_whitelist: list[str] | None = None
    context_seed: list[Message] | None = None
    memory_scope: MemoryScope = MemoryScope.ISOLATED
    token_budget: int = 4096
    max_iterations: int = 10
    deadline_ms: int = 60000
    allow_spawn_children: bool = False


class Artifact(BaseModel):
    artifact_type: str = ""
    name: str = ""
    uri: str | None = None
    content: dict | str | None = None
    metadata: dict | None = None


class SubAgentHandle(BaseModel):
    sub_agent_id: str = ""
    spawn_id: str = ""
    parent_run_id: str = ""
    status: Literal[
        "PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"
    ] = "PENDING"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SubAgentResult(BaseModel):
    spawn_id: str = ""
    success: bool = False
    final_answer: str | None = None
    error: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    iterations_used: int = 0
    duration_ms: int = 0
    trace_ref: str | None = None


class DelegationSummary(BaseModel):
    status: str = ""
    summary: str = ""
    artifacts_digest: list[str] = Field(default_factory=list)
    error_code: str | None = None
