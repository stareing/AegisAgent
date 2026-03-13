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


class StopSignal(BaseModel):
    reason: StopReason
    message: str | None = None


class IterationError(BaseModel):
    error_type: str = ""
    error_message: str = ""
    retryable: bool = False
    stacktrace: str | None = None


class IterationResult(BaseModel):
    iteration_index: int = 0
    model_response: ModelResponse | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)
    tool_execution_meta: list[ToolExecutionMeta] = Field(default_factory=list)
    stop_signal: StopSignal | None = None
    error: IterationError | None = None


class AgentState(BaseModel):
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


class AgentConfig(BaseModel):
    agent_id: str = "default"
    model_name: str = "gpt-3.5-turbo"
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    max_output_tokens: int = 4096
    max_iterations: int = 20
    allow_spawn_children: bool = False


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
    skill_id: str
    name: str = ""
    description: str = ""
    trigger_keywords: list[str] = Field(default_factory=list)
    system_prompt_addon: str = ""
    model_override: str | None = None
    temperature_override: float | None = None
    recommended_capability_policy_id: str | None = None


# ---------------------------------------------------------------------------
# Run-scoped policies (v2.4 §3 — defect 13)
# These are per-run policies, NOT process-level global config.
# ---------------------------------------------------------------------------

class ContextPolicy(BaseModel):
    """Run-scoped context strategy. Controls compression and history behavior."""

    allow_compression: bool = True
    prefer_recent_history: bool = True
    max_session_groups: int | None = None
    force_include_saved_memory: bool = False


class MemoryPolicy(BaseModel):
    """Run-scoped memory strategy. Controls extraction and save behavior."""

    memory_enabled: bool = True
    auto_extract: bool = True
    allow_overwrite_pinned: bool = False
    allow_auto_save_from_tools: bool = False


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
