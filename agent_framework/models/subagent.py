"""Sub-agent data models — unified status machine, delegation events, HITL, checkpoint.

v3.1 Long-term Interaction with boundary refinements:
- Primary status + PauseReason (orthogonal dimensions, §2)
- WaitMode + allow_intermediate_events (§3)
- AckLevel on events (§4)
- HITL ownership on parent control plane (§6)
- resume vs restart-from-checkpoint distinction (§7)
- CheckpointLevel (§8)
- CANCELLING cooperative state (§9)
- DelegationCapabilities (§10)
- DegradationReason (§16)

Architecture invariant: Long-term sub-tasks are NOT independent run systems.
They are delegation objects under the parent run's control plane.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agent_framework.models.message import Message, TokenUsage

# ---------------------------------------------------------------------------
# Core Enums
# ---------------------------------------------------------------------------

class SpawnMode(str, Enum):
    EPHEMERAL = "EPHEMERAL"
    FORK = "FORK"
    LONG_LIVED = "LONG_LIVED"


class SpawnContextMode(str, Enum):
    """Controls how much parent context a child agent receives.

    MINIMAL: Only the task_input as a single user message.
    PARENT_CONTEXT: Filtered parent session (no tool/delegation messages).
    """

    MINIMAL = "MINIMAL"
    PARENT_CONTEXT = "PARENT_CONTEXT"


class MemoryScope(str, Enum):
    ISOLATED = "ISOLATED"
    INHERIT_READ = "INHERIT_READ"
    SHARED_WRITE = "SHARED_WRITE"


# ---------------------------------------------------------------------------
# Delegation control — two orthogonal dimensions (boundary §3)
# ---------------------------------------------------------------------------

class WaitMode(str, Enum):
    """How the parent waits for the sub-agent (call return strategy).

    BLOCKING: Parent blocks until sub-agent reaches terminal or paused state.
    NON_BLOCKING: Parent gets spawn_id immediately; sub-agent runs in background.
    """

    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"


class CollectionStrategy(str, Enum):
    """How the Lead agent collects results from multiple spawned sub-agents.

    SEQUENTIAL (Mode A): Collect one result at a time. Lead gets a decision
        window after each. Good for dependent tasks needing mid-course correction.
    BATCH_ALL (Mode B): Wait for all spawns to complete, collect all at once.
        Good for independent tasks where only the merged result matters.
    HYBRID (Mode C, default): Each pull returns ALL currently-completed results.
        Degrades to SEQUENTIAL when 1 completes, to BATCH_ALL when all complete
        simultaneously. Recommended default for most orchestration scenarios.
    """

    SEQUENTIAL = "SEQUENTIAL"
    BATCH_ALL = "BATCH_ALL"
    HYBRID = "HYBRID"


class DelegationMode(str, Enum):
    """Backward-compatible single-field delegation mode.

    Preserved for backward compatibility. New code should use
    SubAgentSpec.wait_mode + SubAgentSpec.allow_intermediate_events instead.
    """

    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"
    INTERACTIVE = "INTERACTIVE"


# ---------------------------------------------------------------------------
# Unified SubAgentStatus — primary status + PauseReason (boundary §2/§9)
#
# Design: status tracks the primary lifecycle state, PauseReason explains WHY
# the agent is paused. This avoids state explosion from encoding blocking
# source into the status enum directly.
#
# WAITING_PARENT/WAITING_USER/SUSPENDED are kept as enum values for backward
# compat but they all represent the PAUSED primary state with different
# pause reasons. The is_paused_status() helper classifies them.
# ---------------------------------------------------------------------------

class SubAgentStatus(str, Enum):
    """Unified status for sub-agent lifecycle — local and A2A.

    Primary lifecycle states:
        PENDING → QUEUED → SCHEDULED → RUNNING → terminal
        RUNNING → PAUSED variant (WAITING_PARENT/WAITING_USER/SUSPENDED)
        PAUSED variant → RESUMING → RUNNING
        ANY_ACTIVE → CANCELLING → CANCELLED

    Paused variants (all are "non-running recoverable"):
        WAITING_PARENT: blocked on parent supplemental input
        WAITING_USER: blocked on user confirmation/answer
        SUSPENDED: blocked on external event or checkpoint pause

    Whether a paused agent has released execution resources is defined
    by the runtime, not inferable from status name alone.

    Boundary §9: CANCELLING is cooperative — runtime may stay in
    CANCELLING briefly during non-preemptable operations.
    """

    # Scheduler-owned
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    REJECTED = "REJECTED"

    # Runtime-owned — active
    RUNNING = "RUNNING"

    # Paused variants (§2: primary=PAUSED, distinguished by PauseReason)
    WAITING_PARENT = "WAITING_PARENT"
    WAITING_USER = "WAITING_USER"
    SUSPENDED = "SUSPENDED"

    # Transition states
    RESUMING = "RESUMING"
    CANCELLING = "CANCELLING"  # §9: cooperative cancel in progress

    # LONG_LIVED: task done but agent alive, awaiting send_message
    IDLE = "IDLE"

    # Terminal
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEGRADED = "DEGRADED"
    TIMEOUT = "TIMEOUT"


class PauseReason(str, Enum):
    """Why a sub-agent is paused (orthogonal to primary status, §2).

    Complements SubAgentStatus by explaining the blocking source.
    A SubAgentHandle in any paused state (WAITING_PARENT, WAITING_USER,
    SUSPENDED) SHOULD carry a PauseReason for clarity.
    """

    NONE = "NONE"
    WAIT_PARENT_INPUT = "WAIT_PARENT_INPUT"
    WAIT_USER_INPUT = "WAIT_USER_INPUT"
    WAIT_EXTERNAL_EVENT = "WAIT_EXTERNAL_EVENT"
    CHECKPOINT_PAUSE = "CHECKPOINT_PAUSE"
    QUOTA_BACKPRESSURE = "QUOTA_BACKPRESSURE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class DegradationReason(str, Enum):
    """Why a sub-agent entered DEGRADED state (§16).

    DEGRADED is a terminal state flag, not an intermediate state.
    The reason clarifies what specifically was degraded.
    """

    READ_ONLY_FALLBACK = "READ_ONLY_FALLBACK"
    NO_INTERACTIVE_SUPPORT = "NO_INTERACTIVE_SUPPORT"
    QUOTA_LIMITED = "QUOTA_LIMITED"
    NO_RESUME_CAPABILITY = "NO_RESUME_CAPABILITY"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    PARTIAL_COMPLETION = "PARTIAL_COMPLETION"


# Terminal states — once entered, no further transitions allowed
_TERMINAL_STATES: frozenset[SubAgentStatus] = frozenset({
    SubAgentStatus.COMPLETED,
    SubAgentStatus.FAILED,
    SubAgentStatus.CANCELLED,
    SubAgentStatus.REJECTED,
    SubAgentStatus.DEGRADED,
    SubAgentStatus.TIMEOUT,
})

# Paused states — all represent "non-running recoverable"
_PAUSED_STATES: frozenset[SubAgentStatus] = frozenset({
    SubAgentStatus.WAITING_PARENT,
    SubAgentStatus.WAITING_USER,
    SubAgentStatus.SUSPENDED,
})

# Active states — can be cancelled
_ACTIVE_STATES: frozenset[SubAgentStatus] = frozenset({
    SubAgentStatus.PENDING,
    SubAgentStatus.QUEUED,
    SubAgentStatus.SCHEDULED,
    SubAgentStatus.RUNNING,
    SubAgentStatus.WAITING_PARENT,
    SubAgentStatus.WAITING_USER,
    SubAgentStatus.SUSPENDED,
    SubAgentStatus.RESUMING,
    SubAgentStatus.CANCELLING,
    SubAgentStatus.IDLE,
})

# Allowed state transitions (from -> set of valid targets)
_ALLOWED_TRANSITIONS: dict[SubAgentStatus, frozenset[SubAgentStatus]] = {
    SubAgentStatus.PENDING: frozenset({
        SubAgentStatus.QUEUED, SubAgentStatus.RUNNING,
        SubAgentStatus.REJECTED, SubAgentStatus.CANCELLED,
    }),
    SubAgentStatus.QUEUED: frozenset({
        SubAgentStatus.SCHEDULED, SubAgentStatus.REJECTED, SubAgentStatus.CANCELLED,
    }),
    SubAgentStatus.SCHEDULED: frozenset({
        SubAgentStatus.RUNNING, SubAgentStatus.REJECTED, SubAgentStatus.CANCELLED,
    }),
    SubAgentStatus.RUNNING: frozenset({
        SubAgentStatus.WAITING_PARENT, SubAgentStatus.WAITING_USER,
        SubAgentStatus.SUSPENDED,
        SubAgentStatus.COMPLETED, SubAgentStatus.FAILED,
        SubAgentStatus.TIMEOUT, SubAgentStatus.DEGRADED,
        SubAgentStatus.CANCELLING, SubAgentStatus.CANCELLED,
        SubAgentStatus.IDLE,  # LONG_LIVED: task done but agent alive
    }),
    SubAgentStatus.WAITING_PARENT: frozenset({
        SubAgentStatus.RESUMING, SubAgentStatus.CANCELLING,
        SubAgentStatus.CANCELLED, SubAgentStatus.TIMEOUT, SubAgentStatus.FAILED,
    }),
    SubAgentStatus.WAITING_USER: frozenset({
        SubAgentStatus.RESUMING, SubAgentStatus.CANCELLING,
        SubAgentStatus.CANCELLED, SubAgentStatus.TIMEOUT, SubAgentStatus.FAILED,
    }),
    SubAgentStatus.SUSPENDED: frozenset({
        SubAgentStatus.RESUMING, SubAgentStatus.CANCELLING,
        SubAgentStatus.CANCELLED, SubAgentStatus.TIMEOUT, SubAgentStatus.FAILED,
    }),
    SubAgentStatus.RESUMING: frozenset({
        SubAgentStatus.RUNNING, SubAgentStatus.CANCELLING,
        SubAgentStatus.CANCELLED, SubAgentStatus.FAILED,
    }),
    SubAgentStatus.IDLE: frozenset({
        SubAgentStatus.RUNNING,     # send_message wakes up
        SubAgentStatus.CANCELLING,  # close_agent
        SubAgentStatus.CANCELLED,   # direct cancel
    }),
    SubAgentStatus.CANCELLING: frozenset({
        SubAgentStatus.CANCELLED, SubAgentStatus.FAILED,
    }),
    # Terminal states — no transitions out
    SubAgentStatus.COMPLETED: frozenset(),
    SubAgentStatus.FAILED: frozenset(),
    SubAgentStatus.CANCELLED: frozenset(),
    SubAgentStatus.REJECTED: frozenset(),
    SubAgentStatus.DEGRADED: frozenset(),
    SubAgentStatus.TIMEOUT: frozenset(),
}


def is_terminal_status(status: SubAgentStatus) -> bool:
    """Check whether a status is terminal (no further transitions)."""
    return status in _TERMINAL_STATES


def is_active_status(status: SubAgentStatus) -> bool:
    """Check whether a status is active (can be cancelled)."""
    return status in _ACTIVE_STATES


def is_paused_status(status: SubAgentStatus) -> bool:
    """Check whether a status represents a paused (non-running recoverable) state."""
    return status in _PAUSED_STATES


class InvalidStatusTransitionError(Exception):
    """Raised when attempting a prohibited status transition."""

    def __init__(self, from_status: SubAgentStatus, to_status: SubAgentStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid status transition: {from_status.value} -> {to_status.value}"
        )


def validate_status_transition(
    current: SubAgentStatus, target: SubAgentStatus
) -> None:
    """Validate a status transition. Raises InvalidStatusTransitionError if invalid."""
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStatusTransitionError(current, target)


# Backward-compatible alias — old code using SubAgentTaskStatus
SubAgentTaskStatus = SubAgentStatus


# ---------------------------------------------------------------------------
# Checkpoint Level (boundary §8)
# ---------------------------------------------------------------------------

class CheckpointLevel(str, Enum):
    """What level of recovery a checkpoint supports.

    resume_token is only an entry handle. Whether true resume is possible
    depends on this level declared by the runtime.

    NONE: No checkpoint — restart only.
    COORDINATION_ONLY: Parent knows stage; child restarts from scratch.
    PHASE_RESTARTABLE: Child can restart from a phase boundary.
    STEP_RESUMABLE: Child can resume mid-execution from exact step.
    """

    NONE = "NONE"
    COORDINATION_ONLY = "COORDINATION_ONLY"
    PHASE_RESTARTABLE = "PHASE_RESTARTABLE"
    STEP_RESUMABLE = "STEP_RESUMABLE"


# ---------------------------------------------------------------------------
# Delegation Capabilities (boundary §10: A2A must declare capabilities)
# ---------------------------------------------------------------------------

class DelegationCapabilities(BaseModel):
    """Capabilities declared by a delegation target (local or A2A).

    A2A adapters MUST populate this from the remote agent's capability
    advertisement. Local subagent runtime fills it from config.
    Consumers MUST NOT assume capabilities beyond what is declared.
    """

    supports_progress_events: bool = True
    supports_typed_questions: bool = False
    supports_suspend_resume: bool = False
    supports_checkpointing: bool = False
    supports_artifact_streaming: bool = False
    checkpoint_level: CheckpointLevel = CheckpointLevel.NONE


# ---------------------------------------------------------------------------
# Config Override
# ---------------------------------------------------------------------------

class SubAgentConfigOverride(BaseModel):
    """Typed whitelist for sub-agent config overrides.

    Prohibited overrides (enforced by absence from this model):
    - max_iterations, allow_spawn_children, concurrency limits, quota
    """

    model_name: str | None = None
    temperature: float | None = None
    system_prompt_addon: str | None = None


# ---------------------------------------------------------------------------
# SubAgentSpec — with split delegation dimensions (§3)
# ---------------------------------------------------------------------------

class SubAgentSpec(BaseModel):
    parent_run_id: str = ""
    spawn_id: str = ""
    mode: SpawnMode = SpawnMode.EPHEMERAL
    # Delegation control — two orthogonal dimensions (§3)
    wait_mode: WaitMode = WaitMode.BLOCKING
    allow_intermediate_events: bool = False
    # Backward-compat: delegation_mode maps to wait_mode + allow_intermediate_events
    delegation_mode: DelegationMode = DelegationMode.BLOCKING
    task_input: str = ""
    config_override: SubAgentConfigOverride | None = None
    skill_id: str | None = None
    tool_category_whitelist: list[str] | None = None
    context_seed: list[Message] | None = None
    context_mode: SpawnContextMode = SpawnContextMode.MINIMAL
    memory_scope: MemoryScope = MemoryScope.ISOLATED
    token_budget: int = 4096
    max_iterations: int = 10
    deadline_ms: int = 60000
    allow_spawn_children: bool = False


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

class Artifact(BaseModel):
    """A referenceable result product from an agent or sub-agent run."""

    artifact_type: str = ""
    name: str = ""
    uri: str | None = None
    content: dict | str | None = None
    metadata: dict | None = None


class ArtifactRef(BaseModel):
    """Lightweight reference to a sub-agent artifact for parent consumption."""
    name: str = ""
    artifact_type: str = ""
    uri: str | None = None


# ---------------------------------------------------------------------------
# SubAgentHandle — extended for long-term interaction
# ---------------------------------------------------------------------------

class SubAgentHandle(BaseModel):
    """Handle to a running or completed sub-agent.

    Extended with pause_reason (§2), resume_token, last_event_seq.
    """

    sub_agent_id: str = ""
    spawn_id: str = ""
    parent_run_id: str = ""
    status: SubAgentStatus = SubAgentStatus.PENDING
    pause_reason: PauseReason = PauseReason.NONE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_event_seq: int = 0
    waiting_reason: str | None = None
    resume_token: str | None = None
    capabilities: DelegationCapabilities = Field(default_factory=DelegationCapabilities)

    @model_validator(mode="after")
    def _validate_pause_reason_required(self) -> "SubAgentHandle":
        """Paused states MUST carry a non-NONE pause_reason for explicitness."""
        if is_paused_status(self.status) and self.pause_reason == PauseReason.NONE:
            raise ValueError(
                f"SubAgentHandle with paused status {self.status.value} "
                f"requires an explicit pause_reason (got PauseReason.NONE)"
            )
        return self


# ---------------------------------------------------------------------------
# Suspend / Resume models (boundary §7/§8)
# ---------------------------------------------------------------------------

class SubAgentSuspendReason(str, Enum):
    """Why a sub-agent is suspended. Maps to PauseReason for status tracking."""

    WAIT_PARENT_INPUT = "WAIT_PARENT_INPUT"
    WAIT_USER_CONFIRMATION = "WAIT_USER_CONFIRMATION"
    WAIT_EXTERNAL_EVENT = "WAIT_EXTERNAL_EVENT"
    CHECKPOINT_PAUSE = "CHECKPOINT_PAUSE"


class SubAgentSuspendInfo(BaseModel):
    """Information about a suspended sub-agent, needed to resume it.

    Boundary §7: resume_token is only an entry handle, not a full state
    snapshot. Whether true resume (vs restart-from-checkpoint) is possible
    depends on the runtime's declared CheckpointLevel.

    Boundary §8: checkpoint_level declares what the token actually supports.
    """

    reason: SubAgentSuspendReason
    message: str = ""
    resume_token: str = ""
    checkpoint_level: CheckpointLevel = CheckpointLevel.COORDINATION_ONLY
    payload: dict | None = None


# ---------------------------------------------------------------------------
# SubAgentRawResult
# ---------------------------------------------------------------------------

class SubAgentRawResult(BaseModel):
    """Internal-only raw result from a sub-agent run (Layer 0).

    MUST NOT be exposed to parent LLM or parent prompt context.
    """

    spawn_id: str = ""
    success: bool = False
    final_answer: str | None = None
    error: str | None = None
    raw_iteration_history: list = Field(default_factory=list)
    raw_session_messages: list = Field(default_factory=list)
    internal_error_trace: str | None = None
    debug_metadata: dict | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    iterations_used: int = 0
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# SubAgentResult — extended with suspend_info, final_status, degradation_reason
# ---------------------------------------------------------------------------

class SubAgentResult(BaseModel):
    """Parent-runtime-visible result of a sub-agent run (Layer 1).

    Boundary §14: This model may represent BOTH terminal outcomes and
    continuation states (paused/waiting). When suspend_info is set,
    the agent is paused, not finished.

    Delegation return layering:
    Layer 0 — SubAgentRawResult: Internal-only.
    Layer 1 — SubAgentResult: For PARENT RUNTIME.
    Layer 2 — DelegationSummary: LLM-visible projection.
    Layer 3 — AgentRunResult.artifacts: Promoted artifact descriptors.
    """

    spawn_id: str = ""
    success: bool = False
    final_status: SubAgentStatus = SubAgentStatus.COMPLETED
    final_answer: str | None = None
    error: str | None = None
    suspend_info: SubAgentSuspendInfo | None = None
    degradation_reason: DegradationReason | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    iterations_used: int = 0
    duration_ms: int = 0
    trace_ref: str | None = None
    error_code: str | None = None


# ---------------------------------------------------------------------------
# Delegation Error Codes
# ---------------------------------------------------------------------------

class DelegationErrorCode(str, Enum):
    """Unified error codes for both local subagent and remote A2A delegation."""

    TIMEOUT = "TIMEOUT"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DELEGATION_FAILED = "DELEGATION_FAILED"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"


# Error code → SubAgentStatus mapping
_ERROR_CODE_TO_STATUS: dict[str, SubAgentStatus] = {
    DelegationErrorCode.TIMEOUT: SubAgentStatus.TIMEOUT,
    DelegationErrorCode.QUOTA_EXCEEDED: SubAgentStatus.REJECTED,
    DelegationErrorCode.PERMISSION_DENIED: SubAgentStatus.REJECTED,
    DelegationErrorCode.DELEGATION_FAILED: SubAgentStatus.FAILED,
    DelegationErrorCode.REMOTE_UNAVAILABLE: SubAgentStatus.FAILED,
}


def resolve_delegation_status(
    result: SubAgentResult, error_code: str | None = None
) -> SubAgentStatus:
    """Resolve the unified delegation status from result and error code."""
    if result.final_status not in (SubAgentStatus.COMPLETED, SubAgentStatus.FAILED):
        return result.final_status
    if result.success:
        return SubAgentStatus.COMPLETED
    if result.suspend_info is not None:
        return SubAgentStatus.SUSPENDED
    if error_code:
        return _ERROR_CODE_TO_STATUS.get(error_code, SubAgentStatus.FAILED)
    return SubAgentStatus.FAILED


# ---------------------------------------------------------------------------
# SubAgentTaskRecord — uses unified SubAgentStatus
# ---------------------------------------------------------------------------

class SubAgentTaskRecord(BaseModel):
    """Unified task record tracking a sub-agent from scheduling through execution."""

    subagent_task_id: str = ""
    parent_run_id: str = ""
    status: SubAgentStatus = SubAgentStatus.QUEUED
    child_run_id: str | None = None
    scheduler_decision_ref: str = ""
    runtime_handle_ref: str | None = None
    spawn_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Delegation Event System (PRD §6)
# ---------------------------------------------------------------------------

class DelegationEventType(str, Enum):
    """Types of events in the parent-child interaction channel."""

    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    QUESTION = "QUESTION"
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"
    CHECKPOINT = "CHECKPOINT"
    ARTIFACT_READY = "ARTIFACT_READY"
    SUSPENDED = "SUSPENDED"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AckLevel(str, Enum):
    """What level of acknowledgment an event has received (boundary §4).

    NONE: Event not yet acknowledged.
    RECEIVED: Parent has durably received the event.
    PROJECTED: Parent has projected the event into its context/state.
    HANDLED: Parent has completed business processing (e.g., answered HITL).

    ack_event() in InteractionChannel sets RECEIVED.
    Projection and handling are tracked by the coordinator, not the channel.
    """

    NONE = "NONE"
    RECEIVED = "RECEIVED"
    PROJECTED = "PROJECTED"
    HANDLED = "HANDLED"


class DelegationEvent(BaseModel):
    """A single structured event in the parent-child interaction channel.

    Events are append-only. sequence_no is per-spawn_id and strictly monotonic.
    Parent consumes these via SubAgentInteractionChannel, not via EventBus.

    Boundary §13: Events are classified as observational or committed:
    - Observational: PROGRESS, QUESTION, SUSPENDED (execution observations)
    - Committed: ARTIFACT_READY, COMPLETED (only after commit chain confirms)
    Consumers must not treat observational events as committed state changes.
    """

    event_id: str = ""
    spawn_id: str = ""
    parent_run_id: str = ""
    event_type: DelegationEventType = DelegationEventType.STARTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sequence_no: int = 0
    payload: dict = Field(default_factory=dict)
    requires_ack: bool = False
    ack_level: AckLevel = AckLevel.NONE

    # Backward-compat property
    @property
    def acked(self) -> bool:
        return self.ack_level != AckLevel.NONE


# ---------------------------------------------------------------------------
# HITL models (PRD §9, boundary §6)
#
# Ownership: HITLRequest pending queue belongs to PARENT RUN control plane.
# Sub-agents may only propose requests via QUESTION/CONFIRMATION events.
# Sub-agents do NOT own the user-facing pending request truth table.
# ---------------------------------------------------------------------------

class HITLRequest(BaseModel):
    """A human-in-the-loop request from a sub-agent, forwarded via parent.

    Ownership (boundary §6): The pending queue of HITLRequests belongs to
    the parent run's control plane, NOT the sub-agent session. This is
    because: (1) users only see the parent conversation, (2) sub-agents
    may be cancelled while requests are pending, (3) multiple children
    may have concurrent HITL requests requiring parent-level arbitration.

    Flow: sub-agent QUESTION event → DelegationExecutor → HITLRequest
    → parent coordinator pending queue → user interface → HITLResponse
    → resume_subagent()
    """

    request_id: str = ""
    spawn_id: str = ""
    parent_run_id: str = ""
    request_type: Literal["question", "confirmation", "clarification"] = "question"
    title: str = ""
    message: str = ""
    options: list[str] = Field(default_factory=list)
    suggested_default: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HITLResponse(BaseModel):
    """Response to a HITL request, provided by the user through the parent."""

    request_id: str = ""
    response_type: Literal["answer", "confirm", "deny", "cancel"] = "answer"
    answer: str | None = None
    selected_option: str | None = None


# ---------------------------------------------------------------------------
# Checkpoint (boundary §7/§8)
#
# resume_token is only a handle, NOT equivalent to a full state snapshot.
# Whether true resume is possible depends on checkpoint_level declared by
# the runtime. Only when runtime declares STEP_RESUMABLE can the token be
# used for true mid-execution resume. Otherwise it is restart-from-checkpoint.
# ---------------------------------------------------------------------------

class SubAgentCheckpoint(BaseModel):
    """Lightweight checkpoint for sub-agent suspend/resume.

    Boundary §8: checkpoint_level declares what this checkpoint actually
    supports. COORDINATION_ONLY means the parent knows the stage but the
    child would restart. STEP_RESUMABLE means true mid-execution resume.
    """

    checkpoint_id: str = ""
    spawn_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resume_token: str = ""
    checkpoint_level: CheckpointLevel = CheckpointLevel.COORDINATION_ONLY
    state_ref: str | None = None
    summary: str = ""
    iteration_index: int = 0
    context_snapshot_ref: str | None = None


# ---------------------------------------------------------------------------
# DelegationEventSummary — parent-visible projection of child events (§11)
#
# Boundary §5: This is the DECISION projection for parent agent/coordinator.
# A separate NARRATIVE projection for user-visible display should be built
# by the presentation layer, not overloaded onto this model.
# ---------------------------------------------------------------------------

class DelegationEventSummary(BaseModel):
    """Decision-oriented summary of sub-agent events for parent coordinator.

    Only summaries enter the parent Session — never raw events, full sessions,
    or tool traces from the child. This model serves parent decision-making.
    User-facing narrative should be constructed separately by the UI layer.
    """

    spawn_id: str = ""
    status: SubAgentStatus = SubAgentStatus.RUNNING
    pause_reason: PauseReason = PauseReason.NONE
    summary: str = ""
    question: str | None = None
    checkpoint_notice: str | None = None
    error_code: str | None = None
    degradation_reason: DegradationReason | None = None
    artifacts_digest: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RuntimeNotification — unified notification for background + delegation
#
# Boundary §11: Background tasks and delegation events share the delivery
# pipeline but NOT the semantic model. Background payloads are system
# completion notices; delegation payloads carry structured spawn_id/seq/etc.
# ---------------------------------------------------------------------------

class RuntimeNotificationType(str, Enum):
    BACKGROUND_TASK = "background_task"
    DELEGATION_EVENT = "delegation_event"


class RuntimeNotification(BaseModel):
    """Unified notification envelope for background tasks and delegation events.

    Shares delivery pipeline (RuntimeNotificationChannel.drain_all),
    but payload contracts are type-specific and NOT interchangeable.
    """

    notification_id: str = ""
    notification_type: RuntimeNotificationType = RuntimeNotificationType.BACKGROUND_TASK
    run_id: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# DelegationSummary — LLM-visible projection (Layer 2)
# ---------------------------------------------------------------------------

class DelegationSummary(BaseModel):
    status: str = ""
    summary: str = ""
    artifacts_digest: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error_code: str | None = None


# ---------------------------------------------------------------------------
# ResolvedSubAgentRuntimeBundle
# ---------------------------------------------------------------------------

class ResolvedSubAgentRuntimeBundle(BaseModel):
    """Pre-resolved configuration for sub-agent assembly."""

    resolved_model_name: str = "gpt-3.5-turbo"
    resolved_temperature: float = 1.0
    resolved_system_prompt: str = ""
    resolved_memory_scope: str = "ISOLATED"
    resolved_tool_names: list[str] = Field(default_factory=list)
    resolved_max_iterations: int = 10
    resolved_allow_spawn_children: bool = False
    scheduler_decision_ref: str = ""
    parent_run_id: str = ""
    spawn_id: str = ""
