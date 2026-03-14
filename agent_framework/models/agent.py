from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from agent_framework.models.message import ModelResponse, TokenUsage
from agent_framework.models.subagent import Artifact
from agent_framework.models.tool import ToolExecutionMeta, ToolResult


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    TOOL_CALLING = "TOOL_CALLING"
    SPAWNING = "SPAWNING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"


class StopReason(str, Enum):
    LLM_STOP = "LLM_STOP"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    USER_CANCEL = "USER_CANCEL"
    CUSTOM = "CUSTOM"
    ERROR = "ERROR"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"


class TerminationKind(str, Enum):
    """Classifies WHY a run terminated (v2.5.2 §20).

    NORMAL  — expected completion (LLM_STOP, CUSTOM)
    ABORT   — hard failure, run cannot continue (ERROR, USER_CANCEL)
    DEGRADE — soft limit hit, result may be partial (MAX_ITERATIONS, OUTPUT_TRUNCATED)
    """

    NORMAL = "NORMAL"
    ABORT = "ABORT"
    DEGRADE = "DEGRADE"


# Mapping from StopReason → TerminationKind (authoritative, single source)
_STOP_REASON_TO_TERMINATION_KIND: dict[StopReason, TerminationKind] = {
    StopReason.LLM_STOP: TerminationKind.NORMAL,
    StopReason.CUSTOM: TerminationKind.NORMAL,
    StopReason.ERROR: TerminationKind.ABORT,
    StopReason.USER_CANCEL: TerminationKind.ABORT,
    StopReason.MAX_ITERATIONS: TerminationKind.DEGRADE,
    StopReason.OUTPUT_TRUNCATED: TerminationKind.DEGRADE,
}


class StopSignal(BaseModel):
    reason: StopReason
    message: str | None = None

    @property
    def termination_kind(self) -> TerminationKind:
        """Derived from reason — not a stored field. Single source of truth."""
        return _STOP_REASON_TO_TERMINATION_KIND[self.reason]

    @property
    def is_normal(self) -> bool:
        return self.termination_kind == TerminationKind.NORMAL

    @property
    def is_abort(self) -> bool:
        return self.termination_kind == TerminationKind.ABORT

    @property
    def is_degrade(self) -> bool:
        return self.termination_kind == TerminationKind.DEGRADE


# ---------------------------------------------------------------------------
# Decision models (v2.5.2 §19)
# BaseAgent decision interfaces return these structured types instead of
# bare bools, enabling audit trails and reason tracking.
# ---------------------------------------------------------------------------

class StopDecision(BaseModel):
    """Returned by should_stop(). Replaces bare bool.

    v2.6.1 §31: All decision types must carry source (originating layer)
    and reason (human-readable explanation). Bare bools are prohibited.
    """

    should_stop: bool = False
    reason: str = ""
    source: str = "agent"
    # If should_stop is True and a StopSignal is provided, it takes precedence
    # over auto-generated signals in the coordinator.
    stop_signal: StopSignal | None = None


class ToolCallDecision(BaseModel):
    """Returned by on_tool_call_requested(). Replaces bare bool.

    v2.6.1 §31: Must carry reason and source for audit trail.
    """

    allowed: bool = True
    reason: str = ""
    source: str = "agent"
    normalized_tool_name: str | None = None


class SpawnDecision(BaseModel):
    """Returned by on_spawn_requested(). Replaces bare bool.

    v2.6.1 §31: Must carry reason and source for audit trail.
    """

    allowed: bool = True
    reason: str = ""
    source: str = "agent"


class IterationError(BaseModel):
    error_type: str = ""
    error_message: str = ""
    retryable: bool = False
    stacktrace: str | None = None


class IterationAttempt(BaseModel):
    """Tracks a single attempt within a logical iteration (v2.6.5 §50).

    Retry produces a NEW attempt — never overwrites the original.
    Attempts form a version chain via parent_attempt_id.

    Rules:
    - iteration_id = logical turn (stable across retries)
    - attempt_id = specific execution attempt (unique per try)
    - parent_attempt_id links to the failed attempt that triggered this retry
    - Context compression MUST NOT discard the attempt chain
    - User/model projection may show only the final successful attempt
    - Audit/debug layer MUST preserve the full chain
    """

    attempt_id: str = ""
    iteration_id: str = ""
    parent_attempt_id: str | None = None
    attempt_index: int = 0
    trigger_reason: str = ""


class TransactionGroupAttempt(BaseModel):
    """Tracks a single execution attempt of a transaction group (v2.6.5 §50).

    When tools within a transaction group are retried, a new
    TransactionGroupAttempt is created. The original group record
    is never overwritten.

    Rules:
    - transaction_group_id = logical group (stable)
    - group_attempt_id = specific execution attempt (unique per try)
    - parent_group_attempt_id links to the prior failed attempt
    - Audit must preserve all attempts; projection may show only final
    """

    group_attempt_id: str = ""
    transaction_group_id: str = ""
    parent_group_attempt_id: str | None = None
    attempt_index: int = 0
    status: str = ""  # "pending" | "completed" | "failed" | "retried"
    message_refs: list[str] = Field(default_factory=list)


class IterationResult(BaseModel):
    iteration_index: int = 0
    llm_input_preview: str | None = None
    model_response: ModelResponse | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)
    tool_execution_meta: list[ToolExecutionMeta] = Field(default_factory=list)
    stop_signal: StopSignal | None = None
    error: IterationError | None = None
    # v2.6.5 §50: Optional attempt tracking for retry version chains
    attempt: IterationAttempt | None = None


class AgentState(BaseModel):
    """Mutable run-level state. DOMAIN MODEL — internal to framework.

    DTO boundary (v2.5.2 §27):
    - AgentState is a DOMAIN MODEL owned by RunStateController.
    - It is NEVER exposed to integration layer or external APIs directly.
    - AgentRunResult is the OUTPUT DTO — the only run result type
      that crosses the framework boundary.
    - If an integration layer needs run progress, it should subscribe
      to EventBus events, not read AgentState directly.
    """

    run_id: str = ""
    task: str = ""
    status: AgentStatus = AgentStatus.IDLE
    iteration_count: int = 0
    turn_count: int = 0
    total_tokens_used: int = 0
    active_skill_id: str | None = None
    spawn_count: int = 0
    iteration_history: list[IterationResult] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """Final result of an agent run.

    None semantics (project-wide convention):
    - None = "the field semantically does not exist for this result"
    - final_answer: None = agent did not produce a final answer (error/cancel)
    - error: None = no error occurred (success path)
    - Failure is expressed via error field + success=False, NEVER via None
    - Empty collections use [] not None (iteration_history, artifacts)
    - "Not yet generated" is internal-only; final DTOs always have a value

    Termination semantics (v2.6.1 §32):
    - termination_kind: derived from stop_signal, classifies stop/abort/degrade
    - termination_source: which layer triggered termination
    - Audit logs MUST be able to distinguish stop vs abort vs degrade
    """

    run_id: str = ""
    success: bool = False
    final_answer: str | None = None
    stop_signal: StopSignal = Field(
        default_factory=lambda: StopSignal(reason=StopReason.LLM_STOP)
    )
    usage: TokenUsage = Field(default_factory=TokenUsage)
    iterations_used: int = 0
    iteration_history: list[IterationResult] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    error: str | None = None
    # v2.6.1 §32: Explicit termination classification for audit
    termination_source: str = "runtime"

    @property
    def termination_kind(self) -> TerminationKind:
        """Derived from stop_signal — single source of truth."""
        return self.stop_signal.termination_kind


class AgentConfig(BaseModel):
    """Agent configuration.

    Quota semantics:
    - max_iterations: HARD — exceeded → forced stop (MAX_ITERATIONS)
    - max_output_tokens: SOFT — LLM may truncate, reported as OUTPUT_TRUNCATED
    - allow_spawn_children: HARD — False → spawn denied (PERMISSION_DENIED)
    """

    agent_id: str = "default"
    model_name: str = "gpt-3.5-turbo"
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    max_output_tokens: int = 4096
    max_iterations: int = 20
    allow_spawn_children: bool = False
    max_concurrent_tool_calls: int = 5
    allow_parallel_tool_calls: bool = True


class CapabilityPolicy(BaseModel):
    allowed_tool_categories: list[str] | None = None
    blocked_tool_categories: list[str] | None = None
    allow_network_tools: bool = True
    allow_system_tools: bool = True
    allow_spawn: bool = False
    max_spawn_depth: int = 0
    # §11.10: Memory admin tools (remember/forget/list_memories) are dangerous
    # and default-blocked even if manually registered. Only exposed when True.
    allow_memory_admin: bool = False
    # §12: Policy-level confirmation escalation. Tools in these categories
    # require user confirmation even if ToolMeta.require_confirm=False.
    # Decision hierarchy: force_confirm_categories > ToolMeta.require_confirm > default(no).
    force_confirm_categories: list[str] | None = None


class ErrorStrategy(str, Enum):
    RETRY = "RETRY"
    SKIP = "SKIP"
    ABORT = "ABORT"


class Skill(BaseModel):
    """Skill definition — supports both config-based and file-based skills.

    Config-based (legacy): trigger_keywords + system_prompt_addon
    File-based (SKILL.md): source_path + description-based LLM matching

    For file-based skills, the body is lazy-loaded from source_path on
    first invocation. Only name + description are held in memory at rest.
    """

    model_config = {"arbitrary_types_allowed": True}

    skill_id: str
    name: str = ""
    description: str = ""
    trigger_keywords: list[str] = Field(default_factory=list)
    system_prompt_addon: str = ""
    model_override: str | None = None
    temperature_override: float | None = None
    recommended_capability_policy_id: str | None = None
    # --- File-based skill fields ---
    source_path: str | None = None
    allowed_tools: list[str] | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    argument_hint: str = ""


# ---------------------------------------------------------------------------
# Run-scoped policies (v2.4 §3 — defect 13)
# These are per-run policies, NOT process-level global config.
# ---------------------------------------------------------------------------

class ContextPolicy(BaseModel):
    """Run-scoped context strategy. Controls compression behavior.

    Sole consumer: ContextEngineer (via apply_context_policy).
    RunCoordinator passes this policy but NEVER reads its fields.
    """

    allow_compression: bool = True
    force_include_saved_memory: bool = False


class MemoryPolicy(BaseModel):
    """Run-scoped memory strategy. Controls extraction and save behavior.

    Sole consumer: MemoryManager (via apply_memory_policy).
    RunCoordinator passes this policy but NEVER reads its fields.
    """

    memory_enabled: bool = True
    auto_extract: bool = True
    max_in_context: int = 10
    allow_overwrite_pinned: bool = False


class MemoryQuota(BaseModel):
    """Hard limits for memory storage. Enforced by BaseMemoryManager.remember()."""

    max_items_per_user: int = 200
    max_content_length: int = 2000
    max_tags_per_item: int = 10


class EffectiveRunConfig(BaseModel):
    """Final effective config for a single run.

    Built by RunPolicyResolver from AgentConfig + Skill override.
    Skill override can only modify whitelisted fields (model_name, temperature).

    Invariants:
    - Frozen after construction — no module may modify it during a run.
    - Only RunPolicyResolver creates instances.
    - This is a static configuration snapshot, NOT a state object.
      Runtime statistics (token counts, iteration progress) belong in AgentState.
    - Downstream modules (AgentLoop, ContextEngineer) may READ but never WRITE.
    """

    model_config = {"frozen": True}

    model_name: str = "gpt-3.5-turbo"
    temperature: float = 0.7
    max_output_tokens: int = 4096
    max_iterations: int = 20
    reserve_for_output: int = 1024
    max_concurrent_tool_calls: int = 5
    subagent_token_budget: int = 4096
    allow_parallel_tool_calls: bool = True
