from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.agent.loop import AgentLoop
from agent_framework.models.agent import (
    AgentRunResult,
    AgentState,
    AgentStatus,
    EffectiveRunConfig,
    IterationResult,
    Skill,
    StopReason,
    StopSignal,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, TokenUsage
from agent_framework.models.session import SessionState
from agent_framework.models.subagent import Artifact

# Default global run timeout (5 minutes). Prevents hangs from slow models.
DEFAULT_RUN_TIMEOUT_MS = 300_000

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
        initial_session_messages: list[Message] | None = None,
        run_timeout_ms: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentRunResult:
        run_id = str(uuid.uuid4())
        agent_state = self._initialize_state(agent, task, run_id)
        session_state = SessionState(session_id=str(uuid.uuid4()), run_id=run_id)
        if initial_session_messages:
            for msg in initial_session_messages:
                session_state.append_message(msg)

        timeout_ms = run_timeout_ms or DEFAULT_RUN_TIMEOUT_MS
        run_start = time.monotonic()

        logger.info(
            "run.started",
            run_id=run_id,
            agent_id=agent.agent_id,
            model=agent.agent_config.model_name,
            max_iterations=agent.agent_config.max_iterations,
            allow_spawn=agent.agent_config.allow_spawn_children,
            run_timeout_ms=timeout_ms,
            task=task[:200],
        )

        session_started = False
        try:
            # Defensive reset: clear any stale skill state from a prior run
            # that may not have cleaned up (e.g. SIGKILL). This is safe because
            # _apply_skill_if_needed() below will re-detect and activate as needed.
            self._deactivate_skill_if_needed(deps)

            # Begin session
            deps.memory_manager.begin_session(run_id, agent.agent_id, None)
            session_started = True

            # Skill detection
            self._apply_skill_if_needed(agent, deps, task, agent_state)
            active_skill = deps.skill_router.get_active_skill()
            if active_skill:
                logger.info(
                    "run.skill_activated",
                    run_id=run_id,
                    skill_id=active_skill.skill_id,
                    skill_name=active_skill.name,
                )

            # Build effective config
            effective_config = self._build_effective_config(agent, active_skill)

            await agent.on_before_run(task, agent_state)

            # Iteration loop
            final_answer: str | None = None
            last_stop_signal: StopSignal | None = None

            while True:
                # Global timeout check (wall-clock)
                elapsed_ms = int((time.monotonic() - run_start) * 1000)
                if elapsed_ms >= timeout_ms:
                    logger.warning(
                        "run.timeout",
                        run_id=run_id,
                        elapsed_ms=elapsed_ms,
                        timeout_ms=timeout_ms,
                        iterations_used=agent_state.iteration_count,
                    )
                    last_stop_signal = StopSignal(
                        reason=StopReason.MAX_ITERATIONS,
                        message=f"Run timed out after {elapsed_ms}ms (limit: {timeout_ms}ms)",
                    )
                    break

                # External cancel check
                if cancel_event and cancel_event.is_set():
                    logger.info(
                        "run.cancelled",
                        run_id=run_id,
                        iterations_used=agent_state.iteration_count,
                    )
                    last_stop_signal = StopSignal(
                        reason=StopReason.USER_CANCEL,
                        message="Run cancelled by external signal",
                    )
                    break

                # Prepare LLM request
                llm_request = self._prepare_llm_request(
                    agent, deps, agent_state,
                    session_state=session_state,
                    effective_config=effective_config,
                    active_skill=active_skill,
                    task=task,
                )

                # Execute iteration — pass only minimal deps (v2.4 §5)
                iteration_result = await self._loop.execute_iteration(
                    agent, deps.model_adapter, deps.tool_executor,
                    agent_state, llm_request, effective_config,
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

            await agent.on_final_answer(final_answer, agent_state)

            result = self._finalize_run(
                agent, agent_state, final_answer, last_stop_signal
            )
            elapsed_ms = int((time.monotonic() - run_start) * 1000)
            logger.info(
                "run.finished",
                run_id=run_id,
                success=result.success,
                iterations_used=result.iterations_used,
                total_tokens=result.usage.total_tokens,
                stop_reason=result.stop_signal.reason.value if result.stop_signal else "none",
                elapsed_ms=elapsed_ms,
                answer_preview=(final_answer or "")[:120],
            )
            return result

        except Exception as e:
            logger.error(
                "run.failed",
                run_id=run_id,
                error_type=type(e).__name__,
                error=str(e),
                iterations_used=agent_state.iteration_count,
                total_tokens=agent_state.total_tokens_used,
            )
            return self._handle_run_error(agent, e, agent_state)
        finally:
            if session_started:
                try:
                    deps.memory_manager.end_session()
                except Exception as e:
                    logger.warning("run.end_session_failed", run_id=run_id, error=str(e))
            self._deactivate_skill_if_needed(deps)

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
    ) -> EffectiveRunConfig:
        """Build effective run config from AgentConfig + Skill override.

        v2.4 §8: Skill override can only modify whitelisted fields
        (model_name, temperature). Safety fields like max_iterations are
        never overridden by skills.
        """
        cfg = agent.agent_config
        model_name = cfg.model_name
        temperature = cfg.temperature

        # Skill override — whitelist only (v2.4 §8)
        if active_skill is not None:
            if active_skill.model_override:
                model_name = active_skill.model_override
            if active_skill.temperature_override is not None:
                temperature = active_skill.temperature_override

        return EffectiveRunConfig(
            model_name=model_name,
            temperature=temperature,
            max_output_tokens=cfg.max_output_tokens,
            max_iterations=cfg.max_iterations,
        )

    def _deactivate_skill_if_needed(self, deps: AgentRuntimeDeps) -> None:
        """v2.4 §9/§18: Skill deactivation is RunCoordinator's exclusive responsibility."""
        deps.context_engineer.set_skill_context(None)
        deps.skill_router.deactivate_current_skill()

    @staticmethod
    def _collect_runtime_info() -> dict[str, str]:
        """Collect runtime environment info for context injection."""
        import os
        import platform

        os_map = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}
        return {
            "operating_system": os_map.get(platform.system(), platform.system()),
            "working_directory": os.getcwd(),
        }

    def _prepare_llm_request(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        agent_state: AgentState,
        *,
        session_state: SessionState,
        effective_config: EffectiveRunConfig,
        active_skill: Skill | None,
        task: str,
    ) -> LLMRequest:
        """Build a complete LLM request with context and tool schemas."""
        # Get saved memories
        memories = deps.memory_manager.select_for_context(task, agent_state)

        # Prepare context materials
        # agent_config carries system_prompt etc.; effective_config carries runtime overrides
        context_materials = {
            "agent_config": agent.agent_config,
            "session_state": session_state,
            "memories": memories,
            "task": task,
            "active_skill": active_skill,
            "runtime_info": self._collect_runtime_info(),
        }

        # Build LLM context
        llm_messages = deps.context_engineer.prepare_context_for_llm(
            agent_state, context_materials
        )

        # Export tool schemas with capability ceiling applied first.
        capability_policy = agent.get_capability_policy()
        allowed_tools = apply_capability_policy(
            deps.tool_registry.list_tools(),
            capability_policy,
        )
        tools_schema = deps.tool_registry.export_schemas(
            whitelist=[t.meta.name for t in allowed_tools]
        )

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

        # Promote artifacts from sub-agent delegation results
        promoted_artifacts = self._collect_subagent_artifacts(agent_state)

        return AgentRunResult(
            run_id=agent_state.run_id,
            success=stop_signal.reason in (StopReason.LLM_STOP, StopReason.CUSTOM),
            final_answer=final_answer,
            stop_signal=stop_signal,
            usage=TokenUsage(total_tokens=agent_state.total_tokens_used),
            iterations_used=agent_state.iteration_count,
            iteration_history=list(agent_state.iteration_history),
            artifacts=promoted_artifacts,
        )

    @staticmethod
    def _collect_subagent_artifacts(agent_state: AgentState) -> list[Artifact]:
        """Promote sub-agent artifacts into the parent run result.

        Scans iteration history for spawn_agent tool results containing
        DelegationSummary with artifact_refs, and converts them to Artifact
        objects for the parent AgentRunResult.
        """
        artifacts: list[Artifact] = []
        for iteration in agent_state.iteration_history:
            for tr in iteration.tool_results:
                if tr.tool_name != "spawn_agent" or not tr.success:
                    continue
                output = tr.output
                if not isinstance(output, dict):
                    continue
                for ref in output.get("artifact_refs", []):
                    if isinstance(ref, dict):
                        artifacts.append(Artifact(
                            name=ref.get("name", ""),
                            artifact_type=ref.get("artifact_type", ""),
                            uri=ref.get("uri"),
                        ))
        return artifacts

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
            iteration_history=list(agent_state.iteration_history),
            error=str(error),
        )
