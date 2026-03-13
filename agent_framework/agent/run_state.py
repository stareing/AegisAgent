"""RunStateController — owns all mutations to AgentState and SessionState.

Single responsibility: state mutation. RunCoordinator delegates all state
changes here. Other modules (AgentLoop, ToolExecutor) NEVER directly modify
AgentState or SessionState.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import AgentState, AgentStatus, IterationResult
from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.session import SessionState


class RunStateController:
    """Centralises all AgentState / SessionState mutations.

    Invariants:
    - AgentLoop returns IterationResult, never mutates state directly.
    - ToolExecutor returns ToolResult, never mutates state directly.
    - Only RunStateController calls session_state.append_message().

    iteration_history contract:
    - iteration_history is an APPEND-ONLY structured audit trail.
    - Every iteration MUST be recorded, regardless of outcome
      (success, failure, skip, retry).
    - Retry produces a NEW IterationResult — it never overwrites prior entries.
    - Context compression MUST NOT alter iteration_history. Compression only
      affects the message view sent to the LLM, not the audit record.
    - No module may delete, replace, or reorder entries in iteration_history.
    """

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
    def advance_iteration(
        agent_state: AgentState, iteration_result: IterationResult
    ) -> None:
        """Append a completed iteration to the audit trail.

        This is APPEND-ONLY — every iteration (success, error, retry, skip)
        is recorded. Prior entries are never modified or removed.
        """
        agent_state.iteration_count += 1
        agent_state.iteration_history.append(iteration_result)

    @staticmethod
    def mark_finished(agent_state: AgentState) -> None:
        agent_state.status = AgentStatus.FINISHED

    @staticmethod
    def mark_error(agent_state: AgentState) -> None:
        agent_state.status = AgentStatus.ERROR

    @staticmethod
    def set_active_skill(agent_state: AgentState, skill_id: str) -> None:
        agent_state.active_skill_id = skill_id

    # ------------------------------------------------------------------
    # SessionState mutations — Message projection rules
    # ------------------------------------------------------------------
    #
    # Projection contract (v2.4 §4):
    #   1. assistant message with content and/or tool_calls → 1 Message(role=assistant)
    #   2. Each ToolResult → 1 Message(role=tool), including errors
    #   3. Subagent results → projected as tool message (DelegationSummary in output)
    #   4. Order: assistant → tool_1 → tool_2 → ... (strict, matches LLM expectation)
    #   5. tool errors MUST be projected — never silently dropped
    # ------------------------------------------------------------------

    @staticmethod
    def project_iteration_to_session(
        session_state: SessionState,
        iteration_result: IterationResult,
    ) -> None:
        """Project an IterationResult into SessionState messages.

        This is the ONLY path for writing iteration data into session history.
        """
        # Step 1: assistant message (always project if model responded)
        if iteration_result.model_response:
            resp = iteration_result.model_response
            session_state.append_message(
                Message(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls if resp.tool_calls else None,
                )
            )

        # Step 2: tool results — one message per result, preserving order
        for tr in iteration_result.tool_results:
            # Both success and error are projected as tool messages.
            # Errors must not be silently dropped (v2.4 §4 rule 5).
            output_str = str(tr.output) if tr.success else str(tr.error)
            session_state.append_message(
                Message(
                    role="tool",
                    content=output_str,
                    tool_call_id=tr.tool_call_id,
                    name=tr.tool_name,
                )
            )
