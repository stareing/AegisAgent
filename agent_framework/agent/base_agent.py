from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    CapabilityPolicy,
    ContextPolicy,
    ErrorStrategy,
    IterationResult,
    MemoryPolicy,
    SpawnDecision,
    StopDecision,
    StopReason,
    StopSignal,
    ToolCallDecision,
)
from agent_framework.models.message import ToolCallRequest
from agent_framework.models.tool import ToolResult

if TYPE_CHECKING:
    from agent_framework.models.subagent import SubAgentSpec


class BaseAgent:
    """Base class for agents. Focuses on strategy and hooks.

    Does NOT carry runtime dependencies (those live in AgentRuntimeDeps).

    Hook/decision separation (v2.5.2 §19):

    OBSERVATION HOOKS — fire-and-forget, no control flow influence:
      - on_before_run: called before the run loop starts
      - on_iteration_started: called at the start of each iteration
      - on_tool_call_completed: called after a tool call completes
      - on_final_answer: called when the agent produces a final answer
      These MUST NOT return values that influence orchestration.
      They exist for logging, metrics, and side-effect-free observation.

    DECISION INTERFACES — return structured decisions:
      - on_tool_call_requested → ToolCallDecision (allow/block with reason)
      - on_spawn_requested → SpawnDecision (allow/block with reason)
      - should_stop → StopDecision (stop/continue with optional signal)
      These return typed models, NOT bare bools, enabling audit trails.
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.agent_id = agent_config.agent_id
        self.agent_config = agent_config

    # ---------------------------------------------------------------
    # Observation hooks (no control flow, fire-and-forget)
    # ---------------------------------------------------------------

    async def on_before_run(self, task: str, agent_state: AgentState) -> None:
        """Called before the run loop starts."""

    async def on_iteration_started(
        self, iteration_index: int, agent_state: AgentState
    ) -> None:
        """Called at the start of each iteration."""

    async def on_tool_call_completed(self, tool_result: ToolResult) -> None:
        """Called after a tool call completes."""

    async def on_final_answer(
        self, answer: str | None, agent_state: AgentState
    ) -> None:
        """Called when the agent produces a final answer."""

    # ---------------------------------------------------------------
    # Decision interfaces (return structured decision models)
    # ---------------------------------------------------------------

    async def on_tool_call_requested(
        self, tool_call_request: ToolCallRequest
    ) -> ToolCallDecision:
        """Runtime tool call interceptor. Return ToolCallDecision."""
        return ToolCallDecision(allowed=True)

    async def on_spawn_requested(self, spawn_spec: SubAgentSpec) -> SpawnDecision:
        """Called when a sub-agent spawn is requested."""
        return SpawnDecision(
            allowed=self.agent_config.allow_spawn_children,
            reason="" if self.agent_config.allow_spawn_children else "allow_spawn_children=False",
        )

    def should_stop(
        self, iteration_result: IterationResult, agent_state: AgentState
    ) -> StopDecision:
        """Check if the agent should stop after this iteration."""
        if iteration_result.stop_signal:
            return StopDecision(
                should_stop=True,
                reason=f"stop_signal: {iteration_result.stop_signal.reason.value}",
                stop_signal=iteration_result.stop_signal,
            )
        max_iterations = self.agent_config.max_iterations
        if max_iterations > 0 and agent_state.iteration_count >= max_iterations:
            return StopDecision(
                should_stop=True,
                reason=f"max_iterations ({max_iterations}) reached",
                stop_signal=StopSignal(
                    reason=StopReason.MAX_ITERATIONS,
                    message=f"Reached max iterations ({max_iterations})",
                ),
            )
        return StopDecision(should_stop=False)

    # ---------------------------------------------------------------
    # Strategy methods
    # ---------------------------------------------------------------

    def get_error_policy(
        self, error: Exception, agent_state: AgentState
    ) -> ErrorStrategy | None:
        """Determine error handling strategy. None means use default."""
        return None

    def get_context_policy(self, agent_state: AgentState) -> ContextPolicy:
        return ContextPolicy()

    def get_memory_policy(self, agent_state: AgentState) -> MemoryPolicy:
        return MemoryPolicy()

    def get_capability_policy(self) -> CapabilityPolicy:
        return CapabilityPolicy(
            allow_spawn=self.agent_config.allow_spawn_children,
        )
