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


class SubAgentConfigOverride(BaseModel):
    """Typed whitelist for sub-agent config overrides.

    Only these fields may be overridden. Using a typed model instead of
    a raw dict prevents arbitrary config injection and bypasses.

    Prohibited overrides (enforced by absence from this model):
    - max_iterations (set via SubAgentSpec.max_iterations, not override)
    - allow_spawn_children (forced False by SubAgentFactory)
    - concurrency limits, quota, audit flags, memory governance
    """

    model_name: str | None = None
    temperature: float | None = None
    system_prompt_addon: str | None = None


class SubAgentSpec(BaseModel):
    parent_run_id: str = ""
    spawn_id: str = ""
    mode: SpawnMode = SpawnMode.EPHEMERAL
    task_input: str = ""
    config_override: SubAgentConfigOverride | None = None
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


class SubAgentRawResult(BaseModel):
    """Internal-only raw result from a sub-agent run (v2.5.3 §必修4 Layer 0).

    Contains full execution details for debugging and audit.
    MUST NOT be exposed to parent LLM or parent prompt context.
    MUST NOT be serialized into ToolResult.output.

    This is converted to SubAgentResult by SubAgentRuntime before
    returning to the parent's DelegationExecutor.
    """

    spawn_id: str = ""
    success: bool = False
    final_answer: str | None = None
    error: str | None = None
    # Internal details — never exposed to parent LLM
    raw_iteration_history: list = Field(default_factory=list)
    raw_session_messages: list = Field(default_factory=list)
    internal_error_trace: str | None = None
    debug_metadata: dict | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    iterations_used: int = 0
    duration_ms: int = 0


class SubAgentResult(BaseModel):
    """Parent-runtime-visible result of a sub-agent run.

    Delegation return layering (v2.5.3 §必修4):
    Layer 0 — SubAgentRawResult: Internal-only, full raw details.
              Contains raw session, iteration history, error traces.
              NEVER exposed to parent LLM.
    Layer 1 — SubAgentResult: Structured result for the PARENT RUNTIME.
              Contains success/failure, answer, artifacts, usage, timing.
              Used by SubAgentScheduler, SubAgentRuntime, DelegationExecutor.
    Layer 2 — DelegationSummary: LLM-visible projection of SubAgentResult.
              Contains only text summary + artifact refs + error code.
              Created by DelegationExecutor.summarize_result().
              This is what the model sees in ToolResult.output.
    Layer 3 — AgentRunResult.artifacts: Promoted artifact descriptors.
              RunCoordinator._collect_subagent_artifacts() lifts artifact_refs
              from DelegationSummary into the parent AgentRunResult.

    Flow: SubAgentResult → summarize_result() → DelegationSummary → ToolResult.output
    """

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


class SubAgentTaskStatus(str, Enum):
    """Status of a sub-agent task through its lifecycle.

    Scheduler-owned states: QUEUED, SCHEDULED, REJECTED
    Runtime-owned states: RUNNING, COMPLETED, FAILED, CANCELLED
    """

    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    REJECTED = "REJECTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubAgentTaskRecord(BaseModel):
    """Unified task record tracking a sub-agent from scheduling through execution.

    Ownership boundary (v2.6.3 §39):
    - subagent_task_id: assigned by SubAgentScheduler (never by Runtime)
    - child_run_id: assigned by SubAgentRuntime at actual start (None until then)
    - status: scheduler owns QUEUED/SCHEDULED/REJECTED transitions;
              runtime owns RUNNING/COMPLETED/FAILED/CANCELLED transitions
    - active_children truth source: SubAgentRuntime only
    - scheduler MUST NOT maintain a second active runtime handle set
    """

    subagent_task_id: str = ""
    parent_run_id: str = ""
    status: SubAgentTaskStatus = SubAgentTaskStatus.QUEUED
    child_run_id: str | None = None
    scheduler_decision_ref: str = ""
    runtime_handle_ref: str | None = None
    spawn_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SubAgentStatus(str, Enum):
    """Unified delegation status for both local subagent and A2A (v2.6.4 §44).

    All delegation paths (local SubAgentRuntime, remote A2A) MUST use this
    enum. No delegation implementation may define its own status values.

    Status must be resolved BEFORE DelegationSummary is created.
    Parent run consumes status first, then decides continue/degrade/abort.

    Error code → status mapping:
    - TIMEOUT → FAILED
    - QUOTA_EXCEEDED → REJECTED
    - PERMISSION_DENIED → REJECTED
    - DELEGATION_FAILED → FAILED
    - REMOTE_UNAVAILABLE → FAILED
    - Explicit cancel → CANCELLED

    Prohibited:
    - Using FAILED to represent REJECTED (pre-execution denial)
    - Using DEGRADED to hide real failures
    - Returning error_code without status
    - Different status enums for local vs A2A
    """

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    DEGRADED = "DEGRADED"


# Error code → SubAgentStatus mapping
_ERROR_CODE_TO_STATUS: dict[str, SubAgentStatus] = {
    DelegationErrorCode.TIMEOUT: SubAgentStatus.FAILED,
    DelegationErrorCode.QUOTA_EXCEEDED: SubAgentStatus.REJECTED,
    DelegationErrorCode.PERMISSION_DENIED: SubAgentStatus.REJECTED,
    DelegationErrorCode.DELEGATION_FAILED: SubAgentStatus.FAILED,
    DelegationErrorCode.REMOTE_UNAVAILABLE: SubAgentStatus.FAILED,
}


def resolve_delegation_status(
    result: SubAgentResult, error_code: str | None = None
) -> SubAgentStatus:
    """Resolve the unified delegation status from result and error code.

    Must be called before creating DelegationSummary. The status
    field in DelegationSummary must come from this function.
    """
    if result.success:
        return SubAgentStatus.COMPLETED
    if error_code:
        return _ERROR_CODE_TO_STATUS.get(error_code, SubAgentStatus.FAILED)
    return SubAgentStatus.FAILED


class ResolvedSubAgentRuntimeBundle(BaseModel):
    """Pre-resolved configuration for sub-agent assembly (v2.6.4 §46).

    SubAgentDependencyBuilder/Factory MUST receive this bundle instead of
    interpreting raw SubAgentSpec fields for policy decisions.

    Resolution responsibility:
    - SubAgentPolicyResolver (or equivalent): resolves memory scope details,
      capability upper bounds, effective config, override legality
    - SubAgentScheduler: resolves quota/scheduling decisions
    - SubAgentFactory: ONLY assembles instances from this resolved bundle

    Prohibited for Factory/Builder:
    - Re-interpreting MemoryScope raw enum to decide behavior
    - Merging EffectiveRunConfig from raw fields
    - Patching CapabilityPolicy
    - Overriding quota decisions
    - Expanding tool visibility beyond resolved_tool_names
    """

    resolved_model_name: str = "gpt-3.5-turbo"
    resolved_temperature: float = 0.7
    resolved_system_prompt: str = ""
    resolved_memory_scope: str = "ISOLATED"  # MemoryScope value
    resolved_tool_names: list[str] = Field(default_factory=list)
    resolved_max_iterations: int = 10
    resolved_allow_spawn_children: bool = False  # Always False for sub-agents
    scheduler_decision_ref: str = ""
    parent_run_id: str = ""
    spawn_id: str = ""


class DelegationSummary(BaseModel):
    status: str = ""
    summary: str = ""
    artifacts_digest: list[str] = Field(default_factory=list)
    # Full artifact references for parent to decide on promotion
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error_code: str | None = None
