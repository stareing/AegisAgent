from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    CapabilityPolicy,
    ErrorStrategy,
    IterationResult,
    StopReason,
    StopSignal,
)
from agent_framework.models.message import ToolCallRequest
from agent_framework.models.tool import ToolResult

if TYPE_CHECKING:
    from agent_framework.models.subagent import SubAgentSpec


class ContextPolicy:
    """Policy for context construction."""

    compress_threshold_ratio: float = 0.85
    max_context_tokens: int = 8192
    reserve_for_output: int = 1024


class MemoryPolicy:
    """Policy for memory behavior."""

    enabled: bool = True
    auto_extract: bool = True
    max_in_context: int = 10


class BaseAgent:
    """Base class for agents. Focuses on strategy and hooks.

    Does NOT carry runtime dependencies (those live in AgentRuntimeDeps).
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.agent_id = agent_config.agent_id
        self.agent_config = agent_config

    # ---------------------------------------------------------------
    # Lifecycle hooks
    # ---------------------------------------------------------------

    async def on_before_run(self, task: str, agent_state: AgentState) -> None:
        """Called before the run loop starts."""
        pass

    async def on_iteration_started(
        self, iteration_index: int, agent_state: AgentState
    ) -> None:
        """Called at the start of each iteration."""
        pass

    async def on_tool_call_requested(
        self, tool_call_request: ToolCallRequest
    ) -> bool:
        """Runtime tool call interceptor. Return False to block."""
        return True

    async def on_tool_call_completed(self, tool_result: ToolResult) -> None:
        """Called after a tool call completes."""
        pass

    async def on_spawn_requested(self, spawn_spec: SubAgentSpec) -> bool:
        """Called when a sub-agent spawn is requested. Return False to block."""
        return self.agent_config.allow_spawn_children

    async def on_final_answer(
        self, answer: str | None, agent_state: AgentState
    ) -> None:
        """Called when the agent produces a final answer."""
        pass

    def should_stop(
        self, iteration_result: IterationResult, agent_state: AgentState
    ) -> bool:
        """Check if the agent should stop after this iteration."""
        if iteration_result.stop_signal:
            return True
        if agent_state.iteration_count >= self.agent_config.max_iterations:
            return True
        return False

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
