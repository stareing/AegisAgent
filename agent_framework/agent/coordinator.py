from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.agent.loop import AgentLoop
from agent_framework.models.agent import (
    AgentConfig,
    AgentRunResult,
    AgentState,
    AgentStatus,
    IterationResult,
    Skill,
    StopReason,
    StopSignal,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, TokenUsage
from agent_framework.models.session import SessionState

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps

logger = get_logger(__name__)


class RunCoordinator:
    """Manages the full lifecycle of an agent run.

    Flow (section 13.9):
    1. Initialize state + SessionState
    2. memory_manager.begin_session()
    3. Detect and activate skill
    4. Build effective config
    5. Iteration loop:
       - Get saved memories
       - Get session history
       - Build context
       - Prepare LLM request
       - Execute iteration
       - Write to SessionState
       - Record iteration
       - Check stop
    6. memory_manager.record_turn()
    7. memory_manager.end_session()
    8. Return AgentRunResult
    """

    def __init__(self, loop: AgentLoop | None = None) -> None:
        self._loop = loop or AgentLoop()

    async def run(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        task: str,
    ) -> AgentRunResult:
        run_id = str(uuid.uuid4())
        agent_state = self._initialize_state(agent, task, run_id)
        session_state = SessionState(session_id=str(uuid.uuid4()), run_id=run_id)

        logger.info("run.started", run_id=run_id, agent_id=agent.agent_id, task=task[:100])

        try:
            # Begin session
            deps.memory_manager.begin_session(run_id, agent.agent_id, None)

            # Skill detection
            self._apply_skill_if_needed(agent, deps, task, agent_state)
            active_skill = deps.skill_router.get_active_skill()

            # Build effective config
            effective_config = self._build_effective_config(agent, active_skill)

            await agent.on_before_run(task, agent_state)

            # Iteration loop
            final_answer: str | None = None
            last_stop_signal: StopSignal | None = None

            while True:
                # Prepare LLM request
                llm_request = self._prepare_llm_request(
                    agent, deps, agent_state,
                    session_state=session_state,
                    effective_config=effective_config,
                    active_skill=active_skill,
                    task=task,
                )

                # Execute iteration
                iteration_result = await self._loop.execute_iteration(
                    agent, deps, agent_state, llm_request
                )

                # Write to session state
                self._record_iteration(
                    deps, session_state, iteration_result, agent_state
                )

                agent_state.iteration_count += 1
                agent_state.iteration_history.append(iteration_result)

                # Check stop
                if agent.should_stop(iteration_result, agent_state):
                    if iteration_result.model_response and iteration_result.model_response.content:
                        final_answer = iteration_result.model_response.content
                    last_stop_signal = iteration_result.stop_signal
                    break

            # Post-run
            agent_state.status = AgentStatus.FINISHED
            deps.memory_manager.record_turn(task, final_answer, agent_state.iteration_history)
            deps.memory_manager.end_session()
            deps.skill_router.deactivate_current_skill()

            await agent.on_final_answer(final_answer, agent_state)

            result = self._finalize_run(
                agent, agent_state, final_answer, last_stop_signal
            )
            logger.info("run.finished", run_id=run_id, success=result.success)
            return result

        except Exception as e:
            logger.error("run.failed", run_id=run_id, error=str(e))
            return self._handle_run_error(agent, e, agent_state)

    def _initialize_state(
        self, agent: BaseAgent, task: str, run_id: str
    ) -> AgentState:
        return AgentState(
            run_id=run_id,
            task=task,
            status=AgentStatus.IDLE,
        )

    def _build_effective_config(
        self, agent: BaseAgent, active_skill: Skill | None
    ) -> AgentConfig:
        """Build effective config merging agent config with skill overrides.

        Skill overrides only apply to current run (section 13.5).
        """
        config = agent.agent_config.model_copy()

        if active_skill is not None:
            if active_skill.model_override:
                config.model_name = active_skill.model_override
            if active_skill.temperature_override is not None:
                config.temperature = active_skill.temperature_override

        return config

    def _prepare_llm_request(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        agent_state: AgentState,
        *,
        session_state: SessionState,
        effective_config: AgentConfig,
        active_skill: Skill | None,
        task: str,
    ) -> LLMRequest:
        """Build a complete LLM request with context and tool schemas."""
        # Get saved memories
        memories = deps.memory_manager.select_for_context(task, agent_state)

        # Prepare context materials
        context_materials = {
            "agent_config": effective_config,
            "session_state": session_state,
            "memories": memories,
            "task": task,
            "active_skill": active_skill,
        }

        # Build LLM context
        llm_messages = deps.context_engineer.prepare_context_for_llm(
            agent_state, context_materials
        )

        # Export tool schemas
        tools_schema = deps.tool_registry.export_schemas()

        return LLMRequest(messages=llm_messages, tools_schema=tools_schema)

    def _apply_skill_if_needed(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        task: str,
        agent_state: AgentState,
    ) -> None:
        skill = deps.skill_router.detect_skill(task)
        if skill:
            deps.skill_router.activate_skill(skill, deps.context_engineer)
            agent_state.active_skill_id = skill.skill_id

    def _record_iteration(
        self,
        deps: AgentRuntimeDeps,
        session_state: SessionState,
        iteration_result: IterationResult,
        agent_state: AgentState,
    ) -> None:
        """Record iteration results into session state."""
        # Add assistant message
        if iteration_result.model_response:
            resp = iteration_result.model_response
            session_state.append_message(
                Message(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls if resp.tool_calls else None,
                )
            )

        # Add tool results
        for tr in iteration_result.tool_results:
            output_str = str(tr.output) if tr.success else str(tr.error)
            session_state.append_message(
                Message(
                    role="tool",
                    content=output_str,
                    tool_call_id=tr.tool_call_id,
                    name=tr.tool_name,
                )
            )

    def _finalize_run(
        self,
        agent: BaseAgent,
        agent_state: AgentState,
        final_answer: str | None,
        stop_signal: StopSignal | None,
    ) -> AgentRunResult:
        if stop_signal is None:
            stop_signal = StopSignal(reason=StopReason.LLM_STOP)

        return AgentRunResult(
            run_id=agent_state.run_id,
            success=stop_signal.reason in (StopReason.LLM_STOP, StopReason.CUSTOM),
            final_answer=final_answer,
            stop_signal=stop_signal,
            usage=TokenUsage(total_tokens=agent_state.total_tokens_used),
            iterations_used=agent_state.iteration_count,
        )

    def _handle_run_error(
        self,
        agent: BaseAgent,
        error: Exception,
        agent_state: AgentState,
    ) -> AgentRunResult:
        agent_state.status = AgentStatus.ERROR
        return AgentRunResult(
            run_id=agent_state.run_id,
            success=False,
            stop_signal=StopSignal(
                reason=StopReason.ERROR,
                message=str(error),
            ),
            usage=TokenUsage(total_tokens=agent_state.total_tokens_used),
            iterations_used=agent_state.iteration_count,
            error=str(error),
        )
