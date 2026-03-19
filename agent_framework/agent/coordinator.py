from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.agent.commit_sequencer import CommitSequencer
from agent_framework.agent.loop import AgentLoop, AgentLoopDeps
from agent_framework.agent.run_policy import RunPolicyResolver
from agent_framework.agent.run_state import RunStateController
from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.payloads import (artifact_finalize_payload,
                                            run_error_payload,
                                            run_finish_payload,
                                            run_start_payload)
from agent_framework.infra.logger import get_logger
from agent_framework.infra.telemetry import get_tracing_manager
from agent_framework.models.agent import (AgentRunResult, AgentState,
                                          AgentStatus, EffectiveRunConfig,
                                          IterationResult, Skill, StopDecision,
                                          StopReason, StopSignal)
from agent_framework.models.context import LLMRequest
from agent_framework.models.hook import HookPoint
from agent_framework.models.memory import RunSessionOutcome
from agent_framework.models.message import ContentPart, Message, TokenUsage
from agent_framework.models.session import SessionState
from agent_framework.models.subagent import Artifact
from agent_framework.tools.background import BackgroundNotifier
from agent_framework.tools.notification_channel import \
    RuntimeNotificationChannel

# Default global run timeout (5 minutes). Prevents hangs from slow models.
DEFAULT_RUN_TIMEOUT_MS = 300_000

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps

logger = get_logger(__name__)

_CODE_INVESTIGATION_KEYWORDS = (
    "架构",
    "代码架构",
    "审查",
    "review",
    "code review",
    "实现",
    "源码",
    "真实代码",
    "read the real code",
    "不要看md",
    "不要偷懒",
    "根因",
    "root cause",
    "分析代码",
)


class RunCoordinator:
    """Manages the full lifecycle of an agent run.

    Responsibilities (v2.5.1):
    - Orchestration ONLY — sequencing the run lifecycle steps.
    - Delegates state mutation to RunStateController (sole write-port).
    - Delegates message formatting to MessageProjector (via RunStateController).
    - Delegates config composition to RunPolicyResolver.
    - Delegates iteration execution to AgentLoop (via AgentLoopDeps).

    Policy interpretation uniqueness (v2.5.1 §15):
    - ContextPolicy → ONLY ContextEngineer may interpret its fields
    - MemoryPolicy → ONLY MemoryManager may interpret its fields
    - CapabilityPolicy → ONLY authorization chain may interpret its fields
    - RunCoordinator passes policies, NEVER reads their fields for branching

    Exclusive responsibilities:
    - RunCoordinator: WHEN to activate/deactivate skill (orchestration)
    - RunStateController: HOW to write state + holds active_skill
    - MessageProjector: HOW to format iteration results as messages
    - Three roles must not merge.

    Flow:
    1. Initialize state + SessionState
    2. memory_manager.begin_session()
    3. Detect and activate skill (via RunStateController)
    4. Build effective config (via RunPolicyResolver)
    5. Iteration loop:
       - Prepare LLM request
       - Execute iteration (AgentLoop via AgentLoopDeps)
       - Project + commit to SessionState (RunStateController + MessageProjector)
       - Check stop
    6. memory_manager.record_turn()
    7. memory_manager.end_session()
    8. Return AgentRunResult
    """

    def __init__(self, loop: AgentLoop | None = None) -> None:
        self._loop = loop or AgentLoop()
        self._state_ctrl = RunStateController()
        self._policy_resolver = RunPolicyResolver()
        self._commit_sequencer = CommitSequencer()
        # Cached per-run: tool schemas don't change within a run
        self._cached_tools_schema: list[dict] | None = None
        # Dispatcher for hook dispatch (created per-run from deps.hook_executor)
        self._dispatcher: HookDispatchService | None = None
        # Unified notification channel (v3.1) — instance-level, survives across runs.
        # Handles both background bash tasks AND delegation events.
        self._bg_notifier = BackgroundNotifier()
        self._notification_channel = RuntimeNotificationChannel(
            bg_notifier=self._bg_notifier,
        )

    def _build_user_message(
        self,
        task: str,
        content_parts: list[ContentPart] | None = None,
    ) -> Message:
        """Build the initial user Message, preserving multimodal content_parts."""
        if content_parts:
            return Message(role="user", content=task, content_parts=content_parts)
        return Message(role="user", content=task)

    async def run(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        task: str,
        initial_session_messages: list[Message] | None = None,
        user_id: str | None = None,
        run_timeout_ms: int | None = None,
        cancel_event: asyncio.Event | None = None,
        content_parts: list[ContentPart] | None = None,
    ) -> AgentRunResult:
        _tm = get_tracing_manager()
        run_id = str(uuid.uuid4())
        agent_state = self._state_ctrl.initialize_state(task, run_id)
        session_state = SessionState(session_id=str(uuid.uuid4()), run_id=run_id)
        if initial_session_messages:
            # Use RunStateController — sole write-port for SessionState
            self._state_ctrl.append_projected_messages(
                session_state, initial_session_messages
            )
        # Write user task as first message in session so subsequent iterations
        # see [user] → [assistant+tool] → [tool_result] in correct order.
        self._state_ctrl.append_user_message(
            session_state, self._build_user_message(task, content_parts)
        )

        timeout_ms = run_timeout_ms or DEFAULT_RUN_TIMEOUT_MS
        run_start = time.monotonic()

        # Bind run_id and progressive mode to tool executor
        if hasattr(deps.tool_executor, "set_current_run_id"):
            maybe = deps.tool_executor.set_current_run_id(run_id)
            if inspect.isawaitable(maybe):
                await maybe
        if hasattr(deps.tool_executor, "_progressive_mode"):
            deps.tool_executor._progressive_mode = getattr(
                agent.agent_config, "progressive_tool_results", False
            )

        _run_span = _tm.start_span("agent.run", attributes={
            "run_id": run_id,
            "agent_id": agent.agent_id,
            "model": agent.agent_config.model_name,
            "max_iterations": agent.agent_config.max_iterations,
            "task": task[:200],
        })

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

        # Reset per-run caches
        self._cached_tools_schema = None
        # Reset frozen summary from previous run (prevents cross-run leakage)
        if hasattr(deps.context_engineer, "reset_compressor"):
            deps.context_engineer.reset_compressor()

        # Begin stateful adapter session for KV cache optimization.
        # If a conversation-level session is already active (begun by
        # AgentFramework.begin_conversation), skip begin/end here so the
        # session persists across multiple run() calls — enabling the
        # "first-run full context, subsequent-runs delta only" pattern.
        adapter = deps.model_adapter
        adapter_stateful = False
        conversation_session_active = (
            hasattr(adapter, "_session") and adapter._session.active
        )
        if not conversation_session_active and hasattr(adapter, "begin_session"):
            maybe = adapter.begin_session(session_id=run_id)
            if inspect.isawaitable(maybe):
                await maybe
        sfn = getattr(adapter, "supports_stateful_session", None)
        if sfn:
            try:
                val = sfn()
                if inspect.isawaitable(val):
                    val = await val
                adapter_stateful = bool(val)
            except Exception:
                pass
        if adapter_stateful:
            logger.info(
                "run.stateful_session",
                run_id=run_id,
                conversation_session=conversation_session_active,
            )
        session_started = False
        try:
            # Defensive reset: clear any stale skill state from a prior run
            # that may not have cleaned up (e.g. SIGKILL).
            self._state_ctrl.deactivate_skill()
            deps.context_engineer.set_skill_context(None)

            # Begin memory session (v2.6.3 §41: paired with end_run_session in finally)
            deps.memory_manager.begin_run_session(run_id, agent.agent_id, user_id)
            session_started = True

            # Skill detection — router detects, RunStateController holds state
            self._detect_and_activate_skill(deps, agent_state)
            active_skill = self._state_ctrl.active_skill
            if active_skill:
                logger.info(
                    "run.skill_activated",
                    run_id=run_id,
                    skill_id=active_skill.skill_id,
                    skill_name=active_skill.name,
                )

            # Build complete policy bundle (delegated to RunPolicyResolver — sole source)
            policy_bundle = self._policy_resolver.resolve_run_policy_bundle(
                agent, active_skill, agent_state
            )
            effective_config = policy_bundle.effective_run_config

            # Pass policies to their authorized consumers (v2.6.1 §30)
            # MemoryPolicy → MemoryManager (sole interpreter)
            deps.memory_manager.apply_memory_policy(policy_bundle.memory_policy)
            # ContextPolicy → ContextEngineer (sole interpreter)
            deps.context_engineer.apply_context_policy(policy_bundle.context_policy)

            await agent.on_before_run(task, agent_state)

            # RUN_START hook
            _hook_exec = deps.hook_executor
            self._dispatcher = HookDispatchService(_hook_exec) if _hook_exec is not None else None
            if self._dispatcher is not None:
                try:
                    await self._dispatcher.fire(
                        HookPoint.RUN_START,
                        run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                        payload=run_start_payload(task, agent.agent_config.model_name),
                    )
                except Exception as hook_err:
                    logger.warning("run.hook_run_start_failed", error=str(hook_err))

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

                # Drain background task notifications before LLM call (s08)
                await self._drain_background_notifications(session_state, deps)

                # Prepare LLM request
                # Give executor a fresh session snapshot so spawn can build context seed.
                if hasattr(deps.tool_executor, "set_current_session_messages"):
                    maybe = deps.tool_executor.set_current_session_messages(
                        session_state.get_messages()
                    )
                    if inspect.isawaitable(maybe):
                        await maybe

                llm_request = await self._prepare_llm_request(
                    agent, deps, agent_state,
                    session_state=session_state,
                    effective_config=effective_config,
                    active_skill=active_skill,
                    task=task,
                )

                # Set status to RUNNING via RunStateController (sole write-port)
                self._state_ctrl.set_status(agent_state, AgentStatus.RUNNING)

                # Execute iteration — pass only AgentLoopDeps (v2.5.1 §13)
                loop_deps = AgentLoopDeps(
                    model_adapter=deps.model_adapter,
                    tool_executor=deps.tool_executor,
                )
                iteration_result = await self._loop.execute_iteration(
                    agent, loop_deps,
                    agent_state, llm_request, effective_config,
                )

                # Progressive mode: tools already executed in parallel,
                # but results are streamed to LLM one by one.
                # Each tool result gets its own session projection + LLM round-trip.
                progressive = getattr(effective_config, "progressive_tool_results", False)
                if (
                    progressive
                    and len(iteration_result.tool_results) > 1
                    and not iteration_result.stop_signal
                ):
                    prog_should_break = False
                    async for item in self._progressive_stream(
                        agent, deps, agent_state, session_state,
                        iteration_result, effective_config, active_skill, task,
                    ):
                        # StreamEvents are consumed silently in non-streaming run()
                        if isinstance(item, dict) and item.get("_progressive_outcome"):
                            if item["final_answer"]:
                                final_answer = item["final_answer"]
                            if item["stop_signal"]:
                                last_stop_signal = item["stop_signal"]
                            prog_should_break = item["should_break"]
                    if prog_should_break:
                        break
                    continue

                # Apply iteration result: token counting + history append
                # (RunStateController — sole write-port, v2.5.3 §必修1)
                self._state_ctrl.apply_iteration_result(agent_state, iteration_result)

                # Project iteration to session (RunStateController)
                self._state_ctrl.project_iteration_to_session(
                    session_state, iteration_result
                )

                # Track task tool calls for reminder injection
                self._track_todo_round(deps, iteration_result)
                # Register any new background tasks from this iteration (s08)
                self._register_background_tasks(iteration_result)

                # Check stop (returns StopDecision, not bare bool)
                stop_decision = agent.should_stop(iteration_result, agent_state)
                if stop_decision.should_stop:
                    if iteration_result.model_response and iteration_result.model_response.content:
                        final_answer = iteration_result.model_response.content
                    # StopDecision may carry its own stop_signal (e.g. ReAct final answer)
                    last_stop_signal = stop_decision.stop_signal or iteration_result.stop_signal
                    break

            # Post-run
            # NOTE: Do NOT clear _bg_notifier here — tasks may outlive this run.
            # Pending background tasks are drained at the start of the next run.
            self._state_ctrl.mark_finished(agent_state)
            commit_decision = deps.memory_manager.record_turn(
                task, final_answer, agent_state.iteration_history
            )
            if commit_decision and hasattr(commit_decision, 'committed'):
                logger.info(
                    "run.memory_commit",
                    run_id=run_id,
                    committed=commit_decision.committed,
                    reason=commit_decision.reason,
                )

            await agent.on_final_answer(final_answer, agent_state)

            # RUN_FINISH hook
            if self._dispatcher is not None:
                try:
                    await self._dispatcher.fire(
                        HookPoint.RUN_FINISH,
                        run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                        payload=run_finish_payload(
                            success=True,
                            iterations_used=agent_state.iteration_count,
                            total_tokens=agent_state.total_tokens_used,
                            final_answer_preview=final_answer or "",
                        ),
                    )
                except Exception as hook_err:
                    logger.warning("run.hook_run_finish_failed", error=str(hook_err))

            result = self._finalize_run(
                agent, agent_state, final_answer, last_stop_signal
            )
            elapsed_ms = int((time.monotonic() - run_start) * 1000)
            _run_span.set_attributes({
                "success": True,
                "iterations_used": result.iterations_used,
                "total_tokens": result.usage.total_tokens,
                "elapsed_ms": elapsed_ms,
            })
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
            _run_span.record_exception(e)
            logger.error(
                "run.failed",
                run_id=run_id,
                error_type=type(e).__name__,
                error=str(e),
                iterations_used=agent_state.iteration_count,
                total_tokens=agent_state.total_tokens_used,
            )
            # RUN_ERROR hook
            if self._dispatcher is not None:
                await self._dispatcher.fire_advisory(
                    HookPoint.RUN_ERROR,
                    run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                    payload=run_error_payload(
                        error_type=type(e).__name__,
                        error_message=str(e),
                        iterations_used=agent_state.iteration_count,
                    ),
                )
            # Contract: failed runs also go through record_turn for CommitDecision
            # (base_manager.py §41: failed runs decide memory via CommitDecision)
            try:
                deps.memory_manager.record_turn(
                    task, final_answer, agent_state.iteration_history
                )
            except Exception as mem_err:
                logger.warning(
                    "run.failed_memory_commit_error",
                    run_id=run_id,
                    error=str(mem_err),
                )
            return self._handle_run_error(agent, e, agent_state)
        finally:
            if session_started:
                try:
                    # v2.6.3 §41: end_run_session() always in finally
                    outcome = RunSessionOutcome(
                        status=(
                            "completed" if agent_state.status == AgentStatus.FINISHED
                            else "aborted"
                        ),
                        termination_kind=(
                            last_stop_signal.termination_kind.value
                            if last_stop_signal else "NORMAL"
                        ),
                        termination_reason=str(
                            last_stop_signal.message if last_stop_signal else ""
                        ),
                        audit_ref=run_id,
                    )
                    deps.memory_manager.end_run_session(outcome)
                except Exception as e:
                    logger.warning("run.end_session_failed", run_id=run_id, error=str(e))
            # Cancel any active sub-agents on run exit (B2: cleanup)
            if deps.sub_agent_runtime:
                try:
                    cancelled = await deps.sub_agent_runtime.cancel_all(run_id)
                    if cancelled > 0:
                        logger.info(
                            "run.subagents_cancelled_on_exit",
                            run_id=run_id,
                            cancelled=cancelled,
                        )
                except Exception as e:
                    logger.warning("run.subagent_cancel_failed", run_id=run_id, error=str(e))
            # End stateful adapter session — only if we started it (not conversation-level)
            if not conversation_session_active and hasattr(deps.model_adapter, "end_session"):
                try:
                    maybe = deps.model_adapter.end_session()
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    pass
            # Always deactivate skill — run-scoped, must not leak
            self._state_ctrl.deactivate_skill(agent_state)
            deps.context_engineer.set_skill_context(None)
            _run_span.end()

    async def run_stream(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        task: str,
        initial_session_messages: list[Message] | None = None,
        user_id: str | None = None,
        run_timeout_ms: int | None = None,
        cancel_event: asyncio.Event | None = None,
        content_parts: list[ContentPart] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Streaming variant of run(). Yields StreamEvents in real-time.

        The final event is always StreamEventType.DONE carrying the AgentRunResult,
        or StreamEventType.ERROR on failure.
        All other lifecycle logic (memory, context, policies) is identical to run().
        """
        from agent_framework.models.stream import StreamEvent, StreamEventType

        _tm = get_tracing_manager()
        run_id = str(uuid.uuid4())
        _run_span = _tm.start_span("agent.run_stream", attributes={
            "run_id": run_id,
            "agent_id": agent.agent_id,
            "task": task[:200],
        })
        agent_state = self._state_ctrl.initialize_state(task, run_id)
        session_state = SessionState(session_id=str(uuid.uuid4()), run_id=run_id)
        if initial_session_messages:
            self._state_ctrl.append_projected_messages(
                session_state, initial_session_messages
            )
        self._state_ctrl.append_user_message(
            session_state, self._build_user_message(task, content_parts)
        )

        timeout_ms = run_timeout_ms or DEFAULT_RUN_TIMEOUT_MS
        run_start = time.monotonic()

        if hasattr(deps.tool_executor, "set_current_run_id"):
            maybe = deps.tool_executor.set_current_run_id(run_id)
            if inspect.isawaitable(maybe):
                await maybe
        if hasattr(deps.tool_executor, "_progressive_mode"):
            deps.tool_executor._progressive_mode = getattr(
                agent.agent_config, "progressive_tool_results", False
            )

        logger.info(
            "run_stream.started",
            run_id=run_id,
            agent_id=agent.agent_id,
            model=agent.agent_config.model_name,
            task=task[:200],
        )

        self._cached_tools_schema = None
        # NOTE: Do NOT recreate _bg_notifier here — it's instance-level,
        # so background tasks from previous runs are still tracked.
        if hasattr(deps.context_engineer, "reset_compressor"):
            deps.context_engineer.reset_compressor()

        adapter = deps.model_adapter
        adapter_stateful = False
        conversation_session_active = (
            hasattr(adapter, "_session") and adapter._session.active
        )
        if not conversation_session_active and hasattr(adapter, "begin_session"):
            maybe = adapter.begin_session(session_id=run_id)
            if inspect.isawaitable(maybe):
                await maybe
        sfn = getattr(adapter, "supports_stateful_session", None)
        if sfn:
            try:
                val = sfn()
                if inspect.isawaitable(val):
                    val = await val
                adapter_stateful = bool(val)
            except Exception:
                pass

        session_started = False
        final_answer: str | None = None
        last_stop_signal: StopSignal | None = None

        try:
            self._state_ctrl.deactivate_skill()
            deps.context_engineer.set_skill_context(None)
            deps.memory_manager.begin_run_session(run_id, agent.agent_id, user_id)
            session_started = True

            self._detect_and_activate_skill(deps, agent_state)
            active_skill = self._state_ctrl.active_skill

            policy_bundle = self._policy_resolver.resolve_run_policy_bundle(
                agent, active_skill, agent_state
            )
            effective_config = policy_bundle.effective_run_config
            deps.memory_manager.apply_memory_policy(policy_bundle.memory_policy)
            deps.context_engineer.apply_context_policy(policy_bundle.context_policy)

            await agent.on_before_run(task, agent_state)

            # RUN_START hook (stream)
            _hook_exec = deps.hook_executor
            self._dispatcher = HookDispatchService(_hook_exec) if _hook_exec is not None else None
            if self._dispatcher is not None:
                try:
                    await self._dispatcher.fire(
                        HookPoint.RUN_START,
                        run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                        payload=run_start_payload(task, agent.agent_config.model_name),
                    )
                except Exception as hook_err:
                    logger.warning("run_stream.hook_run_start_failed", error=str(hook_err))

            while True:
                elapsed_ms = int((time.monotonic() - run_start) * 1000)
                if elapsed_ms >= timeout_ms:
                    last_stop_signal = StopSignal(
                        reason=StopReason.MAX_ITERATIONS,
                        message=f"Run timed out after {elapsed_ms}ms",
                    )
                    break

                if cancel_event and cancel_event.is_set():
                    last_stop_signal = StopSignal(
                        reason=StopReason.USER_CANCEL,
                        message="Run cancelled by external signal",
                    )
                    break

                # Drain background task notifications before LLM call (s08)
                await self._drain_background_notifications(session_state, deps)

                if hasattr(deps.tool_executor, "set_current_session_messages"):
                    maybe = deps.tool_executor.set_current_session_messages(
                        session_state.get_messages()
                    )
                    if inspect.isawaitable(maybe):
                        await maybe

                llm_request = await self._prepare_llm_request(
                    agent, deps, agent_state,
                    session_state=session_state,
                    effective_config=effective_config,
                    active_skill=active_skill,
                    task=task,
                )

                self._state_ctrl.set_status(agent_state, AgentStatus.RUNNING)
                loop_deps = AgentLoopDeps(
                    model_adapter=deps.model_adapter,
                    tool_executor=deps.tool_executor,
                )

                iteration_result: IterationResult | None = None
                progressive = getattr(effective_config, "progressive_tool_results", False)
                progressive_assistant_projected = False

                async for item in self._loop.execute_iteration_stream(
                    agent, loop_deps,
                    agent_state, llm_request, effective_config,
                ):
                    if isinstance(item, IterationResult):
                        iteration_result = item
                    elif isinstance(item, StreamEvent):
                        yield item  # Forward to consumer immediately

                        if (
                            progressive
                            and item.type == StreamEventType.ASSISTANT_TOOL_CALLS
                            and not progressive_assistant_projected
                        ):
                            self._project_progressive_assistant_message(
                                session_state,
                                item.data.get("content"),
                                item.data.get("tool_calls"),
                            )
                            progressive_assistant_projected = True

                        if progressive and item.type == StreamEventType.PROGRESSIVE_DONE:
                            self._project_progressive_tool_result(
                                session_state=session_state,
                                tool_call_id=item.data.get("tool_call_id"),
                                tool_name=str(item.data.get("tool_name", "spawn_agent")),
                                output_str=str(
                                    item.data.get(
                                        "display_text",
                                        item.data.get("output", ""),
                                    )
                                ),
                            )

                assert iteration_result is not None

                # Progressive run_stream: assistant/tool messages were projected
                # incrementally during streaming. Only finalize state here.
                if (
                    progressive
                    and len(iteration_result.tool_results) > 1
                    and not iteration_result.stop_signal
                ):
                    self._state_ctrl.apply_iteration_result(agent_state, iteration_result)
                    stop_decision = agent.should_stop(iteration_result, agent_state)
                    if stop_decision.should_stop:
                        if (
                            iteration_result.model_response
                            and iteration_result.model_response.content
                        ):
                            final_answer = iteration_result.model_response.content
                        last_stop_signal = stop_decision.stop_signal or iteration_result.stop_signal
                        break
                    continue

                self._state_ctrl.apply_iteration_result(agent_state, iteration_result)
                self._state_ctrl.project_iteration_to_session(
                    session_state, iteration_result
                )

                # Track task tool calls for reminder injection
                self._track_todo_round(deps, iteration_result)
                # Register any new background tasks from this iteration (s08)
                self._register_background_tasks(iteration_result)

                stop_decision = agent.should_stop(iteration_result, agent_state)
                if stop_decision.should_stop:
                    if iteration_result.model_response and iteration_result.model_response.content:
                        final_answer = iteration_result.model_response.content
                    last_stop_signal = stop_decision.stop_signal or iteration_result.stop_signal
                    break

            # Post-run
            # NOTE: Do NOT clear _bg_notifier here — tasks may outlive this run.
            # Pending background tasks are drained at the start of the next run.
            self._state_ctrl.mark_finished(agent_state)
            deps.memory_manager.record_turn(
                task, final_answer, agent_state.iteration_history
            )
            await agent.on_final_answer(final_answer, agent_state)

            # RUN_FINISH hook (stream)
            if self._dispatcher is not None:
                try:
                    await self._dispatcher.fire(
                        HookPoint.RUN_FINISH,
                        run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                        payload=run_finish_payload(
                            success=True,
                            iterations_used=agent_state.iteration_count,
                            total_tokens=agent_state.total_tokens_used,
                            final_answer_preview=final_answer or "",
                        ),
                    )
                except Exception as hook_err:
                    logger.warning("run_stream.hook_run_finish_failed", error=str(hook_err))

            result = self._finalize_run(
                agent, agent_state, final_answer, last_stop_signal
            )
            _run_span.set_attributes({
                "success": True,
                "iterations_used": result.iterations_used,
                "total_tokens": result.usage.total_tokens,
            })
            yield StreamEvent(
                type=StreamEventType.DONE,
                data={"result": result},
            )

        except Exception as e:
            _run_span.record_exception(e)
            logger.error(
                "run_stream.failed",
                run_id=run_id,
                error_type=type(e).__name__,
                error=str(e),
            )
            # RUN_ERROR hook (stream)
            if self._dispatcher is not None:
                await self._dispatcher.fire_advisory(
                    HookPoint.RUN_ERROR,
                    run_id=run_id, agent_id=agent.agent_id, user_id=user_id,
                    payload=run_error_payload(
                        error_type=type(e).__name__,
                        error_message=str(e),
                        iterations_used=agent_state.iteration_count,
                    ),
                )
            try:
                deps.memory_manager.record_turn(
                    task, final_answer, agent_state.iteration_history
                )
            except Exception:
                pass
            yield StreamEvent(
                type=StreamEventType.ERROR,
                data={"error": str(e), "error_type": type(e).__name__},
            )
        finally:
            if session_started:
                try:
                    outcome = RunSessionOutcome(
                        status=(
                            "completed" if agent_state.status == AgentStatus.FINISHED
                            else "aborted"
                        ),
                        termination_kind=(
                            last_stop_signal.termination_kind.value
                            if last_stop_signal else "NORMAL"
                        ),
                        termination_reason=str(
                            last_stop_signal.message if last_stop_signal else ""
                        ),
                        audit_ref=run_id,
                    )
                    deps.memory_manager.end_run_session(outcome)
                except Exception:
                    pass
            if deps.sub_agent_runtime:
                try:
                    await deps.sub_agent_runtime.cancel_all(run_id)
                except Exception:
                    pass
            if not conversation_session_active and hasattr(deps.model_adapter, "end_session"):
                try:
                    maybe = deps.model_adapter.end_session()
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    pass
            self._state_ctrl.deactivate_skill(agent_state)
            deps.context_engineer.set_skill_context(None)
            _run_span.end()

    def _detect_and_activate_skill(
        self,
        deps: AgentRuntimeDeps,
        agent_state: AgentState,
    ) -> None:
        """Detect skill from task and activate it.

        RunCoordinator decides WHEN to activate (orchestration).
        RunStateController holds the active_skill state.
        ContextEngineer receives the prompt injection.
        """
        skill = deps.skill_router.detect_skill(agent_state.task)
        if skill:
            self._state_ctrl.activate_skill(agent_state, skill)
            deps.context_engineer.set_skill_context(skill.system_prompt_addon)

    # Tool names that count as "task write" for reminder tracking
    _TASK_WRITE_TOOLS = frozenset({"task_create", "task_update"})

    @staticmethod
    def _track_todo_round(
        deps: AgentRuntimeDeps,
        iteration_result: IterationResult,
    ) -> None:
        """Check if this iteration called any task tool and update the TaskManager."""
        from agent_framework.tools.todo import TaskService
        executor = deps.tool_executor
        todo_svc = getattr(executor, "_todo_service", None)
        if not isinstance(todo_svc, TaskService):
            return
        run_id = getattr(executor, "_current_run_id", "")
        if not run_id:
            return
        wrote_task = any(
            tr.tool_name in RunCoordinator._TASK_WRITE_TOOLS and tr.success
            for tr in iteration_result.tool_results
        )
        todo_svc.get(run_id).mark_round(wrote_task)

    # ------------------------------------------------------------------
    # Background task auto-notification (s08)
    # ------------------------------------------------------------------

    def _register_background_tasks(self, iteration_result: IterationResult) -> None:
        """Detect bash_exec(run_in_background=True) and spawn_agent(async) results.

        Registers background bash tasks for polling, and monitors spawn_ids
        for delegation event draining.
        """
        for tr in iteration_result.tool_results:
            if tr.tool_name == "bash_exec" and tr.success:
                output = tr.output
                if isinstance(output, dict) and output.get("status") == "running":
                    task_id = output.get("task_id", "")
                    if task_id:
                        self._bg_notifier.register(task_id)
            # Monitor async spawns for delegation event draining
            if tr.tool_name == "spawn_agent" and tr.success:
                output = tr.output
                if isinstance(output, dict):
                    spawn_id = output.get("spawn_id", "")
                    if spawn_id:
                        self._notification_channel.monitor_spawn(spawn_id)

    async def _drain_background_notifications(
        self, session_state: SessionState, deps: AgentRuntimeDeps | None = None,
    ) -> None:
        """Drain all pending notifications (background tasks + delegation events).

        Pipeline:
        1. drain_all() — polls bg tasks + delegation events, ack → RECEIVED
        2. Format and inject into session as user message + assistant ack
        3. Advance delegation events to PROJECTED (boundary §4)
        4. Auto-forward HITL events: QUESTION/CONFIRMATION_REQUEST →
           event_to_hitl_request → forward_hitl_request → resume_subagent,
           then advance to HANDLED (boundary §6)
        """
        if not self._notification_channel.has_pending:
            return

        notifications = self._notification_channel.drain_all()
        if not notifications:
            return

        text = RuntimeNotificationChannel.format_notifications(notifications)
        # Inject as user message + assistant ack (standard message pair)
        self._state_ctrl.append_user_message(
            session_state,
            Message(role="user", content=text),
        )
        self._state_ctrl.append_projected_messages(
            session_state,
            [Message(role="assistant", content="Noted background results.")],
        )

        # Advance delegation events to PROJECTED + auto-forward HITL (boundary §4/§6)
        delegation_executor = deps.delegation_executor if deps else None
        for n in notifications:
            if n.notification_type.value != "delegation_event":
                continue

            spawn_id = n.payload.get("spawn_id", "")
            event_id = n.payload.get("event_id", "")
            event_type = n.payload.get("event_type", "")

            if spawn_id and event_id:
                self._notification_channel.mark_projected(spawn_id, event_id)

            # Auto-forward HITL events to the delegation executor (boundary §6)
            if event_type in ("QUESTION", "CONFIRMATION_REQUEST") and delegation_executor:
                await self._handle_hitl_event(
                    delegation_executor, n, spawn_id, event_id,
                )

    async def _handle_hitl_event(
        self,
        delegation_executor: Any,
        notification: Any,
        spawn_id: str,
        event_id: str,
    ) -> None:
        """Convert a HITL delegation event to HITLRequest, forward, and resume.

        Full chain: event → event_to_hitl_request → forward_hitl_request
        → HITLResponse → resume_subagent → mark_handled
        """
        try:
            from agent_framework.models.subagent import (DelegationEvent,
                                                         DelegationEventType)
            from agent_framework.tools.hitl import event_to_hitl_request

            # Reconstruct a minimal DelegationEvent from the notification payload
            payload = notification.payload
            event_type_str = payload.get("event_type", "")
            event_type = DelegationEventType(event_type_str)
            event = DelegationEvent(
                event_id=event_id,
                spawn_id=spawn_id,
                parent_run_id=payload.get("parent_run_id", ""),
                event_type=event_type,
                payload=payload.get("data", {}),
            )

            hitl_request = event_to_hitl_request(event)
            if hitl_request is None:
                return

            logger.info(
                "coordinator.hitl.auto_forward",
                spawn_id=spawn_id,
                event_id=event_id,
                request_type=hitl_request.request_type,
            )

            response = await delegation_executor.forward_hitl_request(hitl_request)
            if response is None:
                # No HITL handler configured — leave at PROJECTED
                return

            # Resume the sub-agent with the user's response
            resume_payload: dict = {}
            if response.response_type == "answer":
                resume_payload["answer"] = response.answer or response.selected_option or ""
            elif response.response_type == "confirm":
                resume_payload["confirmed"] = True
            elif response.response_type in ("deny", "cancel"):
                resume_payload["denied"] = True

            await delegation_executor.resume_subagent(
                spawn_id, resume_payload, None,
            )

            # Advance to HANDLED — business processing complete (boundary §4)
            self._notification_channel.mark_handled(spawn_id, event_id)

            logger.info(
                "coordinator.hitl.completed",
                spawn_id=spawn_id,
                event_id=event_id,
                response_type=response.response_type,
            )

        except Exception as e:
            logger.warning(
                "coordinator.hitl.auto_forward_failed",
                spawn_id=spawn_id,
                event_id=event_id,
                error=str(e),
            )

    @staticmethod
    def _collect_runtime_info(
        agent: BaseAgent | None = None,
        deps: AgentRuntimeDeps | None = None,
        effective_config: EffectiveRunConfig | None = None,
        agent_state: AgentState | None = None,
        task: str | None = None,
    ) -> dict[str, str]:
        """Collect runtime environment and agent capabilities for context injection.

        Dynamically injects capability constraints so the LLM knows its
        actual limits (spawn, parallel calls, iterations) without
        hardcoding values in prompt templates.
        """
        import os
        import platform

        os_map = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}
        info: dict[str, str] = {
            "operating_system": os_map.get(platform.system(), platform.system()),
            "working_directory": os.getcwd(),
        }

        # Dynamic capability injection
        if effective_config:
            info["max_iterations"] = (
                "unlimited"
                if effective_config.max_iterations <= 0
                else str(effective_config.max_iterations)
            )

        if agent:
            can_spawn = agent.agent_config.allow_spawn_children
            info["can_spawn_subagents"] = str(can_spawn).lower()

        parallel_enabled: bool | None = None
        if effective_config:
            parallel_enabled = bool(effective_config.allow_parallel_tool_calls)

        if deps:
            # Parallel tool call support from model adapter
            adapter = deps.model_adapter
            if hasattr(adapter, "supports_parallel_tool_calls"):
                try:
                    val = adapter.supports_parallel_tool_calls()
                    # AsyncMock returns awaitable — discard in sync context
                    if inspect.isawaitable(val):
                        val.close()  # prevent "coroutine never awaited" warning
                    else:
                        model_parallel_supported = bool(val)
                        if parallel_enabled is None:
                            parallel_enabled = model_parallel_supported
                        else:
                            parallel_enabled = (
                                parallel_enabled and model_parallel_supported
                            )
                except Exception:
                    pass

            # Sub-agent quotas
            if deps.sub_agent_runtime and hasattr(deps.sub_agent_runtime, "_scheduler"):
                sched = deps.sub_agent_runtime._scheduler
                info["max_concurrent_subagents"] = str(sched._max_concurrent)
                info["max_subagents_per_run"] = str(sched._max_per_run)

        if parallel_enabled is not None:
            info["parallel_tool_calls"] = str(parallel_enabled).lower()

        # Live run state — LLM sees current progress
        if agent_state:
            info["current_iteration"] = str(agent_state.iteration_count)
            info["spawned_subagents"] = str(agent_state.spawn_count)

        if task and RunCoordinator._requires_code_investigation(task):
            info["investigation_mode"] = "codebase_analysis"
            info["investigation_expectation"] = (
                "Use glob_files/grep_search before summarizing; read multiple "
                "implementation files and distinguish verified facts from inference."
            )

        # Task state injection — run-scoped via TaskService
        if deps:
            from agent_framework.tools.todo import TaskService
            todo_svc = getattr(deps.tool_executor, "_todo_service", None)
            if isinstance(todo_svc, TaskService):
                run_id = getattr(deps.tool_executor, "_current_run_id", "")
                if run_id:
                    mgr = todo_svc.get(run_id)
                    summary = mgr.summary_text()
                    if summary:
                        info["todo_summary"] = summary
                    if mgr.should_remind():
                        info["todo_reminder"] = (
                            "The task list hasn't been updated recently. "
                            "Consider using task_update to mark progress, "
                            "complete finished tasks, or add new tasks with task_create."
                        )

        return info

    @staticmethod
    def _requires_code_investigation(task: str) -> bool:
        task_lower = task.lower()
        return any(keyword in task_lower for keyword in _CODE_INVESTIGATION_KEYWORDS)

    async def _prepare_llm_request(
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
        # v2.6.4 §45: Pass SessionSnapshot (read-only) to context layer,
        # not the mutable SessionState.
        session_snap = self._state_ctrl.session_snapshot(session_state)

        # Skill descriptions: only inject when file-based skills exist
        # (avoids constant token overhead when only keyword-triggered skills are used)
        skill_descriptions = deps.skill_router.get_skill_descriptions()
        if skill_descriptions and not any(s.get("description") for s in skill_descriptions):
            skill_descriptions = []

        runtime_info = self._collect_runtime_info(
            agent, deps, effective_config, agent_state, task
        )
        if inspect.isawaitable(runtime_info):
            runtime_info = await runtime_info

        # Check if adapter is in stateful session mode
        adapter_stateful = False
        if (hasattr(deps.model_adapter, "_session")
                and getattr(deps.model_adapter._session, "active", False)):
            sfn = getattr(deps.model_adapter, "supports_stateful_session", None)
            if sfn:
                try:
                    val = sfn()
                    if inspect.isawaitable(val):
                        val = await val
                    adapter_stateful = bool(val)
                except Exception:
                    pass

        context_materials = {
            "agent_config": agent.agent_config,
            "session_state": session_snap,
            "memories": memories,
            "task": task,
            "active_skill": active_skill,
            "runtime_info": runtime_info,
            "skill_descriptions": skill_descriptions,
            "stateful_session": adapter_stateful,
            "model_adapter": deps.model_adapter,
            "tool_entries": deps.tool_registry.list_tools() if deps.tool_registry else [],
        }

        # Build LLM context
        # NOTE: ContextPolicy is consumed exclusively by ContextEngineer.
        # RunCoordinator passes it but never interprets its fields.
        llm_messages = await deps.context_engineer.prepare_context_for_llm(
            agent_state, context_materials
        )

        # Tool schemas: cached per-run (tools don't change within a run).
        # VISIBILITY filtering only — security is ToolExecutor.is_tool_allowed().
        if self._cached_tools_schema is None:
            capability_policy = agent.get_capability_policy()
            allowed_tools = apply_capability_policy(
                deps.tool_registry.list_tools(),
                capability_policy,
            )
            self._cached_tools_schema = deps.tool_registry.export_schemas(
                whitelist=[t.meta.name for t in allowed_tools]
            )

        if hasattr(deps.context_engineer, "set_tools_schema_tokens"):
            tools_token_est = deps.context_engineer.set_tools_schema_tokens(
                self._cached_tools_schema or []
            )
        else:
            tools_token_est = 0

        return LLMRequest(
            messages=llm_messages,
            tools_schema=self._cached_tools_schema,
            tools_schema_tokens=tools_token_est,
        )

    async def _progressive_stream(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        agent_state: AgentState,
        session_state: SessionState,
        iteration_result: IterationResult,
        effective_config: EffectiveRunConfig,
        active_skill: Any,
        task: str,
    ) -> AsyncGenerator[Any, None]:
        """Stream tool results to LLM one by one, yielding StreamEvents.

        Tools have already been executed in parallel by execute_iteration.
        This method projects each result individually and yields PROGRESSIVE_DONE
        for real-time UI. Coordinator does not synthesize mid-stream narration.

        Final yield is a _ProgressiveOutcome with stop decision info.
        """
        from agent_framework.models.stream import StreamEvent, StreamEventType

        tool_results = iteration_result.tool_results
        tool_metas = iteration_result.tool_execution_meta
        model_response = iteration_result.model_response
        total = len(tool_results)
        if model_response:
            self._project_progressive_assistant_message(
                session_state,
                model_response.content,
                model_response.tool_calls if model_response.tool_calls else None,
            )

        # Emit PROGRESSIVE_START for each tool
        for i, tr in enumerate(tool_results):
            description = ""
            tool_name = tr.tool_name
            if model_response and model_response.tool_calls:
                for tc in model_response.tool_calls:
                    if tc.id == tr.tool_call_id:
                        description = self._loop._progressive_tool_description(tc)
                        break
            yield StreamEvent(
                type=StreamEventType.PROGRESSIVE_START,
                data={"tool_call_id": tr.tool_call_id, "tool_name": tool_name,
                      "description": description,
                      "index": i + 1, "total": total},
            )

        # Step 2: Feed tool results one by one
        for i, (tr, tm) in enumerate(zip(tool_results, tool_metas)):
            raw = tr.output if tr.success else tr.error
            output_str = str(raw) if raw else ""
            # Human-readable display_text
            if isinstance(raw, dict) and "summary" in raw:
                display_text = str(raw["summary"])
            else:
                display_text = output_str

            self._project_progressive_tool_result(
                session_state=session_state,
                tool_call_id=tr.tool_call_id,
                tool_name=tr.tool_name,
                output_str=display_text,
            )

            # Yield PROGRESSIVE_DONE
            description = ""
            if model_response and model_response.tool_calls:
                for tc in model_response.tool_calls:
                    if tc.id == tr.tool_call_id:
                        description = self._loop._progressive_tool_description(tc)
                        break
            yield StreamEvent(
                type=StreamEventType.PROGRESSIVE_DONE,
                data={
                    "tool_call_id": tr.tool_call_id, "tool_name": tr.tool_name,
                    "description": description,
                    "success": tr.success, "output": output_str,
                    "display_text": display_text,
                    "index": i + 1, "total": total,
                },
            )

        # Record the full iteration
        self._state_ctrl.apply_iteration_result(agent_state, iteration_result)

        # Yield outcome for caller
        stop_decision = agent.should_stop(iteration_result, agent_state)
        outcome = {
            "_progressive_outcome": True,
            "should_break": stop_decision.should_stop,
            "final_answer": None,
            "stop_signal": None,
        }
        if stop_decision.should_stop:
            if model_response and model_response.content:
                outcome["final_answer"] = model_response.content
            outcome["stop_signal"] = stop_decision.stop_signal or iteration_result.stop_signal
        yield outcome

    def _project_progressive_assistant_message(
        self,
        session_state: SessionState,
        content: str | None,
        tool_calls: list[Any] | None,
    ) -> None:
        self._state_ctrl.append_projected_messages(session_state, [Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls or None,
        )])

    def _project_progressive_tool_result(
        self,
        *,
        session_state: SessionState,
        tool_call_id: str | None,
        tool_name: str,
        output_str: str,
    ) -> None:
        """Project a factual progressive tool result into session state."""
        self._state_ctrl.append_projected_messages(session_state, [Message(
            role="tool",
            content=output_str,
            tool_call_id=tool_call_id,
            name=tool_name,
        )])

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

        # ARTIFACT_FINALIZE hook — fires after artifact promotion, before result
        if promoted_artifacts and self._dispatcher is not None:
            for art in promoted_artifacts:
                self._dispatcher.fire_sync_advisory(
                    HookPoint.ARTIFACT_FINALIZE,
                    run_id=agent_state.run_id,
                    payload=artifact_finalize_payload(
                        artifact_name=art.name,
                        artifact_type=art.artifact_type,
                        uri=art.uri or "",
                    ),
                )

        progressive_responses = getattr(agent_state, "_progressive_responses", [])
        return AgentRunResult(
            run_id=agent_state.run_id,
            success=stop_signal.reason in (StopReason.LLM_STOP, StopReason.CUSTOM),
            final_answer=final_answer,
            stop_signal=stop_signal,
            usage=TokenUsage(total_tokens=agent_state.total_tokens_used),
            iterations_used=agent_state.iteration_count,
            iteration_history=list(agent_state.iteration_history),
            artifacts=promoted_artifacts,
            progressive_responses=list(progressive_responses),
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
        self._state_ctrl.mark_error(agent_state)
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
