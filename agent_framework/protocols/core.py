from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable

from agent_framework.models.memory import (
    CommitDecision,
    MemoryCandidate,
    MemoryKind,
    MemoryRecord,
    MemorySourceContext,
    RunSessionOutcome,
)
from agent_framework.models.message import Message, ModelResponse, ToolCallRequest
from agent_framework.models.tool import ToolEntry, ToolExecutionMeta, ToolResult

if TYPE_CHECKING:
    from agent_framework.adapters.model.base_adapter import ModelChunk
    from agent_framework.models.agent import AgentState, IterationResult, Skill
    from agent_framework.models.context import ContextStats
    from agent_framework.models.subagent import SubAgentHandle, SubAgentResult, SubAgentSpec


# ---------------------------------------------------------------------------
# Model Adapter
# ---------------------------------------------------------------------------
@runtime_checkable
class ModelAdapterProtocol(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse: ...

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ModelChunk]: ...

    def count_tokens(self, messages: list[Message]) -> int: ...

    def supports_parallel_tool_calls(self) -> bool: ...


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------
@runtime_checkable
class ToolRegistryProtocol(Protocol):
    def get_tool(self, name: str) -> ToolEntry: ...
    def has_tool(self, name: str) -> bool: ...
    def list_tools(
        self,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> list[ToolEntry]: ...
    def export_schemas(self, whitelist: list[str] | None = None) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Tool Executor
# ---------------------------------------------------------------------------
@runtime_checkable
class ToolExecutorProtocol(Protocol):
    async def execute(
        self, tool_call_request: ToolCallRequest
    ) -> tuple[ToolResult, ToolExecutionMeta]: ...

    async def batch_execute(
        self, tool_call_requests: list[ToolCallRequest]
    ) -> list[tuple[ToolResult, ToolExecutionMeta]]: ...


# ---------------------------------------------------------------------------
# Delegation Executor
# ---------------------------------------------------------------------------
@runtime_checkable
class DelegationExecutorProtocol(Protocol):
    async def delegate_to_subagent(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult: ...

    async def delegate_to_a2a(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult: ...


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------
@runtime_checkable
class MemoryStoreProtocol(Protocol):
    def save(self, record: MemoryRecord) -> str: ...
    def update(self, record: MemoryRecord) -> None: ...
    def delete(self, memory_id: str) -> None: ...
    def get(self, memory_id: str) -> MemoryRecord | None: ...
    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]: ...
    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]: ...
    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]: ...
    def touch(self, memory_id: str) -> None: ...
    def count(self, agent_id: str, user_id: str | None) -> int: ...


# ---------------------------------------------------------------------------
# Memory Manager
# ---------------------------------------------------------------------------
@runtime_checkable
class MemoryManagerProtocol(Protocol):
    """Memory manager protocol.

    Session lifecycle (v2.6.3 §41):
    - begin_run_session() and end_run_session() MUST be paired
    - end_run_session() MUST execute in finally
    - record_turn() returns CommitDecision
    - begin_session/end_session retained as backward-compatible aliases
    """

    def begin_run_session(
        self, run_id: str, agent_id: str, user_id: str | None
    ) -> None: ...
    def end_run_session(
        self, outcome: RunSessionOutcome | None = None
    ) -> None: ...
    def begin_session(
        self, run_id: str, agent_id: str, user_id: str | None
    ) -> None: ...
    def end_session(self) -> None: ...
    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]: ...
    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> CommitDecision: ...
    def remember(
        self,
        candidate: MemoryCandidate,
        source_context: MemorySourceContext | None = None,
    ) -> str | None: ...
    def forget(self, memory_id: str) -> None: ...
    def list_memories(
        self, agent_id: str, user_id: str | None
    ) -> list[MemoryRecord]: ...
    def pin(self, memory_id: str) -> None: ...
    def unpin(self, memory_id: str) -> None: ...
    def activate(self, memory_id: str) -> None: ...
    def deactivate(self, memory_id: str) -> None: ...
    def clear_memories(self, agent_id: str, user_id: str | None) -> int: ...
    def set_enabled(self, enabled: bool) -> None: ...


# ---------------------------------------------------------------------------
# Context Engineer
# ---------------------------------------------------------------------------
@runtime_checkable
class ContextEngineerProtocol(Protocol):
    def prepare_context_for_llm(
        self, agent_state: AgentState, context_materials: dict
    ) -> list[Message]: ...

    def set_skill_context(self, skill_prompt: str | None) -> None: ...

    def build_spawn_seed(
        self,
        session_messages: list[Message],
        query: str,
        token_budget: int,
    ) -> list[Message]: ...

    def report_context_stats(self) -> ContextStats: ...


# ---------------------------------------------------------------------------
# Sub-Agent Runtime
# ---------------------------------------------------------------------------
@runtime_checkable
class SubAgentRuntimeProtocol(Protocol):
    async def spawn(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult: ...

    def get_active_children(
        self, parent_run_id: str
    ) -> list[SubAgentHandle]: ...

    async def cancel_all(self, parent_run_id: str) -> int: ...


# ---------------------------------------------------------------------------
# Confirmation Handler
# ---------------------------------------------------------------------------
@runtime_checkable
class ConfirmationHandlerProtocol(Protocol):
    async def request_confirmation(
        self, tool_name: str, arguments: dict, description: str
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Skill Router
# ---------------------------------------------------------------------------
@runtime_checkable
class SkillRouterProtocol(Protocol):
    """Skill catalog + detection. Does NOT hold per-run activation state.

    Active skill state belongs to run-scoped locals in RunCoordinator,
    not in this shared registry.
    """

    def register_skill(self, skill: Skill) -> None: ...
    def detect_skill(self, user_input: str) -> Skill | None: ...
    def get_skill(self, skill_id: str) -> Skill | None: ...
    def list_skills(self) -> list[Skill]: ...
