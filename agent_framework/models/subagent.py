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
    """A referenceable result product from an agent or sub-agent run.

    Lifecycle contract:
    - Artifact is a DESCRIPTOR, not the payload itself.
    - ``content`` is for small inline results only (< ~10KB).
    - Large objects MUST use ``uri`` (file path or URL) — content should be None.
    - Lifecycle is owned by the PRODUCING runtime (the agent/sub-agent that
      created it). The parent's RunCoordinator may "promote" descriptors into
      its own AgentRunResult.artifacts, but does NOT take ownership of the
      underlying files.
    - Memory layer may absorb an Artifact's summary (via DelegationSummary),
      but NEVER absorbs the artifact body/file. Memory stores metadata only.
    - If the producing runtime is cleaned up, the artifact's backing resource
      may become unavailable — consumers should treat ``uri`` as potentially stale.
    """

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


class ArtifactRef(BaseModel):
    """Lightweight reference to a sub-agent artifact for parent consumption."""
    name: str = ""
    artifact_type: str = ""
    uri: str | None = None


class DelegationErrorCode(str, Enum):
    """Unified error codes for both local subagent and remote A2A delegation.

    The main agent loop sees the same error vocabulary regardless of whether
    the delegation target was a local sub-agent or a remote A2A agent.
    """

    TIMEOUT = "TIMEOUT"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DELEGATION_FAILED = "DELEGATION_FAILED"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"


class DelegationSummary(BaseModel):
    status: str = ""
    summary: str = ""
    artifacts_digest: list[str] = Field(default_factory=list)
    # Full artifact references for parent to decide on promotion
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error_code: str | None = None
