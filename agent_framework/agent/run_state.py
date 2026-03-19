"""RunStateController — the SOLE write-port for run-level state.

Relationship with RunCoordinator (v2.5.3 §必修1):
  - RunCoordinator = orchestrator (decides WHEN to change state)
  - RunStateController = executor (performs HOW state changes)
  - RunCoordinator issues commands; RunStateController executes them.

No other module may directly modify AgentState or SessionState.
AgentLoop returns IterationResult — it does NOT write state.
Only this class calls session_state.append_message().

Prohibited callers:
  - AgentLoop must NOT modify agent_state.status or agent_state.total_tokens_used
  - ToolExecutor must NOT modify AgentState
  - ContextEngineer must NOT modify SessionState
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.agent.message_projector import MessageProjector
from agent_framework.models.agent import (AgentState, AgentStatus,
                                          IterationResult, StopSignal)

if TYPE_CHECKING:
    from agent_framework.models.agent import Skill
    from agent_framework.models.message import Message
    from agent_framework.models.session import SessionSnapshot, SessionState


class AgentStateSnapshot:
    """Immutable snapshot of AgentState for read-only consumers.

    Used by RunCoordinator to expose state to observers without
    granting write access.
    """

    __slots__ = (
        "run_id", "task", "status", "iteration_count",
        "total_tokens_used", "active_skill_id",
    )

    def __init__(self, state: AgentState) -> None:
        self.run_id = state.run_id
        self.task = state.task
        self.status = state.status
        self.iteration_count = state.iteration_count
        self.total_tokens_used = state.total_tokens_used
        self.active_skill_id = state.active_skill_id


class RunStateController:
    """Centralises all AgentState / SessionState mutations.

    Owns:
    - AgentState mutation (iteration, status, tokens, skill)
    - SessionState message commits (via MessageProjector output)
    - active_skill lifecycle (activate / deactivate)

    Does NOT:
    - Interpret ContextPolicy / MemoryPolicy / CapabilityPolicy
    - Call LLM or execute tools
    - Make orchestration decisions

    iteration_history contract:
    - iteration_history is an APPEND-ONLY structured audit trail.
    - Every iteration MUST be recorded (success, failure, skip, retry).
    - Retry produces a NEW IterationResult — never overwrites prior entries.
    - Context compression MUST NOT alter iteration_history.
    - No module may delete, replace, or reorder entries.
    """

    def __init__(self) -> None:
        self._projector = MessageProjector()
        # active_skill is run-scoped state, owned here (not in SkillRouter)
        self._active_skill: Skill | None = None

    # ------------------------------------------------------------------
    # Skill lifecycle
    # ------------------------------------------------------------------

    def activate_skill(
        self, agent_state: AgentState, skill: Skill
    ) -> None:
        """Activate a skill for the current run."""
        self._active_skill = skill
        agent_state.active_skill_id = skill.skill_id

    def deactivate_skill(self, agent_state: AgentState | None = None) -> None:
        """Deactivate the current skill."""
        self._active_skill = None
        if agent_state is not None:
            agent_state.active_skill_id = None

    @property
    def active_skill(self) -> Skill | None:
        return self._active_skill

    # ------------------------------------------------------------------
    # AgentState mutations
    # ------------------------------------------------------------------

    @staticmethod
    def initialize_state(task: str, run_id: str) -> AgentState:
        return AgentState(
            run_id=run_id,
            task=task,
            status=AgentStatus.IDLE,
        )

    @staticmethod
    def set_status(agent_state: AgentState, status: AgentStatus) -> None:
        """Set agent status. Only callable by RunStateController."""
        agent_state.status = status

    @staticmethod
    def add_tokens(agent_state: AgentState, tokens: int) -> None:
        """Increment token usage counter."""
        agent_state.total_tokens_used += tokens

    def apply_iteration_result(
        self,
        agent_state: AgentState,
        iteration_result: IterationResult,
    ) -> None:
        """Apply a completed iteration to AgentState.

        Combines: token counting + iteration history append + spawn counting.
        This is the single entry point for post-iteration state mutation.
        """
        # Token accounting
        if iteration_result.model_response:
            agent_state.total_tokens_used += iteration_result.model_response.usage.total_tokens

        # Spawn counting — track how many sub-agents were spawned this run.
        # Uses ToolExecutionMeta.source to identify delegation calls instead
        # of hardcoding tool names. Falls back to tool_name for compat.
        prev_spawn_count = agent_state.spawn_count
        metas = iteration_result.tool_execution_meta
        for idx, tr in enumerate(iteration_result.tool_results):
            is_spawn = False
            if idx < len(metas) and metas[idx].source == "subagent":
                is_spawn = True
            elif tr.tool_name == "spawn_agent":
                is_spawn = True
            if is_spawn and tr.success:
                agent_state.spawn_count += 1

        # O(1) index: update last_spawn_iteration_index if new spawns this iteration
        if agent_state.spawn_count > prev_spawn_count:
            agent_state.last_spawn_iteration_index = agent_state.iteration_count

        # Append to audit trail (APPEND-ONLY)
        agent_state.iteration_count += 1
        agent_state.iteration_history.append(iteration_result)

    @staticmethod
    def advance_iteration(
        agent_state: AgentState, iteration_result: IterationResult
    ) -> None:
        """Append a completed iteration to the audit trail.

        APPEND-ONLY — every iteration is recorded.
        Prior entries are never modified or removed.

        NOTE: Prefer apply_iteration_result() which also handles token counting.
        This method is retained for backward compatibility.
        """
        agent_state.iteration_count += 1
        agent_state.iteration_history.append(iteration_result)

    @staticmethod
    def mark_finished(agent_state: AgentState) -> None:
        agent_state.status = AgentStatus.FINISHED

    @staticmethod
    def mark_error(agent_state: AgentState) -> None:
        agent_state.status = AgentStatus.ERROR

    def mark_stop(
        self, agent_state: AgentState, stop_signal: StopSignal
    ) -> None:
        """Mark the run as stopped with a specific signal."""
        agent_state.status = AgentStatus.FINISHED

    @staticmethod
    def snapshot(agent_state: AgentState) -> AgentStateSnapshot:
        """Create an immutable snapshot for read-only consumers."""
        return AgentStateSnapshot(agent_state)

    @staticmethod
    def session_snapshot(session_state: SessionState) -> SessionSnapshot:
        """Create an immutable session snapshot for context layer (v2.6.4 §45).

        The context layer must consume this snapshot instead of the mutable
        SessionState to prevent mid-build state changes.
        """
        from agent_framework.models.session import SessionSnapshot
        return SessionSnapshot(session_state)

    # ------------------------------------------------------------------
    # SessionState mutations — delegates formatting to MessageProjector
    # ------------------------------------------------------------------

    def append_user_message(
        self, session_state: SessionState, message: Message
    ) -> None:
        """Append a user message to session. Only write-port for user messages."""
        session_state.append_message(message)

    def append_projected_messages(
        self, session_state: SessionState, messages: list[Message]
    ) -> None:
        """Append pre-projected messages to session."""
        for msg in messages:
            session_state.append_message(msg)

    def project_iteration_to_session(
        self,
        session_state: SessionState,
        iteration_result: IterationResult,
    ) -> None:
        """Project an IterationResult into SessionState messages.

        Formatting is delegated to MessageProjector.
        This method only commits the projected messages.
        """
        messages = self._projector.project_iteration(iteration_result)
        for msg in messages:
            session_state.append_message(msg)
