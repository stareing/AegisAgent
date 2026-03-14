from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.models.agent import (
    AgentState,
    EffectiveRunConfig,
    ErrorStrategy,
    IterationError,
    IterationResult,
    StopReason,
    StopSignal,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import ModelResponse, ToolCallRequest
from agent_framework.models.tool import ToolExecutionMeta, ToolResult

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.protocols.core import ModelAdapterProtocol, ToolExecutorProtocol

logger = get_logger(__name__)

# Safety: force stop if the same tool is called N+ times with identical args
_MAX_REPEATED_TOOL_CALLS = 3
# Safety: force ABORT after N consecutive LLM errors
_MAX_CONSECUTIVE_ERRORS = 3


@dataclass(frozen=True)
class AgentLoopDeps:
    """Minimal dependency set for AgentLoop.

    v2.5.1 §13: AgentLoop MUST NOT receive full AgentRuntimeDeps.
    Only the exact dependencies needed for a single iteration are passed.
    This prevents AgentLoop from becoming a hidden service locator.

    Prohibited additions:
    - memory_manager (belongs to RunCoordinator)
    - skill_router (belongs to RunCoordinator)
    - context_engineer (belongs to RunCoordinator)
    - delegation_executor (belongs to ToolExecutor, not exposed here)
    """

    model_adapter: ModelAdapterProtocol
    tool_executor: ToolExecutorProtocol


class AgentLoop:
    """Executes a single iteration of the agent loop.

    Boundary:
    - Only performs single-iteration execution
    - Consumes AgentLoopDeps (minimal), NOT full AgentRuntimeDeps
    - Does NOT write SessionState (returns IterationResult for caller)
    - Does NOT interpret ContextPolicy/MemoryPolicy
    - Does NOT persist memory or manage skill lifecycle

    One iteration:
    1. Call LLM with prepared request
    2. Check stop conditions
    3. Dispatch tool calls if any
    4. Return structured IterationResult
    """

    async def execute_iteration(
        self,
        agent: BaseAgent,
        loop_deps: AgentLoopDeps,
        agent_state: AgentState,
        llm_request: LLMRequest,
        effective_config: EffectiveRunConfig,
    ) -> IterationResult:
        idx = agent_state.iteration_count

        await agent.on_iteration_started(idx, agent_state)
        logger.info(
            "iteration.started",
            iteration_index=idx,
            run_id=agent_state.run_id,
            max_iterations=effective_config.max_iterations,
            total_tokens_so_far=agent_state.total_tokens_used,
            context_messages=len(llm_request.messages),
            tools_available=len(llm_request.tools_schema) if llm_request.tools_schema else 0,
        )

        # NOTE: Status transitions (RUNNING, TOOL_CALLING) are handled by
        # RunStateController after this method returns. AgentLoop must NOT
        # directly mutate AgentState (v2.5.3 §必修1).

        # 1. Call LLM
        try:
            model_response = await self._call_llm(
                loop_deps.model_adapter,
                llm_request.messages,
                llm_request.tools_schema,
                effective_config,
            )
        except Exception as e:
            return self._handle_iteration_error(agent, e, agent_state, idx)

        # Token accounting is done by RunStateController.apply_iteration_result()
        # after this method returns. AgentLoop must NOT mutate token counters.

        # 2. Check stop conditions
        stop_signal = self._check_stop_conditions(agent, model_response, agent_state)
        if stop_signal:
            logger.info(
                "iteration.stopped",
                iteration_index=idx,
                stop_reason=stop_signal.reason.value,
                stop_message=stop_signal.message or "",
                iteration_tokens=model_response.usage.total_tokens,
                response_preview=(model_response.content or "")[:120],
            )
            return IterationResult(
                iteration_index=idx,
                model_response=model_response,
                stop_signal=stop_signal,
            )

        # 3. Dispatch tool calls
        tool_results: list[ToolResult] = []
        tool_metas: list[ToolExecutionMeta] = []

        if model_response.tool_calls:
            tool_names = [tc.function_name for tc in model_response.tool_calls]
            logger.info(
                "iteration.dispatching_tools",
                iteration_index=idx,
                tool_count=len(model_response.tool_calls),
                tool_names=tool_names,
            )
            tool_results, tool_metas = await self._dispatch_tool_calls(
                agent, loop_deps.tool_executor, model_response.tool_calls, agent_state
            )
            success_count = sum(1 for r in tool_results if r.success)
            fail_count = len(tool_results) - success_count
            total_time = sum(m.execution_time_ms for m in tool_metas)
            logger.info(
                "iteration.tools_done",
                iteration_index=idx,
                success=success_count,
                failed=fail_count,
                total_time_ms=total_time,
            )
        else:
            # Model responded with text but no stop signal and no tool calls
            # — potential no-progress scenario
            logger.warning(
                "iteration.no_tool_no_stop",
                iteration_index=idx,
                finish_reason=model_response.finish_reason,
                response_preview=(model_response.content or "")[:120],
            )

        logger.info(
            "iteration.completed",
            iteration_index=idx,
            iteration_tokens=model_response.usage.total_tokens,
        )

        return IterationResult(
            iteration_index=idx,
            model_response=model_response,
            tool_results=tool_results,
            tool_execution_meta=tool_metas,
        )

    async def _call_llm(
        self,
        model_adapter: ModelAdapterProtocol,
        messages: list,
        tools_schema: list[dict],
        effective_config: EffectiveRunConfig,
    ) -> ModelResponse:
        # Stateful session optimization: send only delta messages
        # when adapter maintains server-side conversation state.
        actual_messages = messages
        is_delta = False
        if hasattr(model_adapter, "get_delta_messages") and hasattr(model_adapter, "_session"):
            try:
                delta = model_adapter.get_delta_messages(messages)
                if isinstance(delta, list) and len(delta) < len(messages):
                    is_delta = True
                    actual_messages = delta
            except Exception:
                pass  # Graceful fallback to full messages

        logger.info(
            "llm.calling",
            model=effective_config.model_name,
            temperature=effective_config.temperature,
            max_output_tokens=effective_config.max_output_tokens,
            message_count=len(actual_messages),
            full_message_count=len(messages),
            is_delta=is_delta,
            tools_count=len(tools_schema) if tools_schema else 0,
        )
        response = await model_adapter.complete(
            messages=actual_messages,
            tools=tools_schema if tools_schema else None,
            temperature=effective_config.temperature,
            max_tokens=effective_config.max_output_tokens,
        )
        tool_names = [tc.function_name for tc in response.tool_calls] if response.tool_calls else []
        logger.info(
            "llm.responded",
            finish_reason=response.finish_reason,
            tool_calls_count=len(response.tool_calls),
            tool_names=tool_names,
            tokens_prompt=response.usage.prompt_tokens,
            tokens_completion=response.usage.completion_tokens,
            tokens_total=response.usage.total_tokens,
            response_preview=(response.content or "")[:100],
        )
        return response

    def _check_stop_conditions(
        self,
        agent: BaseAgent,
        model_response: ModelResponse,
        agent_state: AgentState,
    ) -> StopSignal | None:
        # Normal stop: model says "stop" with no tool calls
        if model_response.finish_reason == "stop" and not model_response.tool_calls:
            logger.debug(
                "stop_check.llm_stop",
                iteration_index=agent_state.iteration_count,
            )
            return StopSignal(reason=StopReason.LLM_STOP)

        # Edge case: some models return finish_reason="stop" WITH tool_calls.
        # Prioritize LLM_STOP — the tool_calls will be discarded.
        if model_response.finish_reason == "stop" and model_response.tool_calls:
            tool_names = [tc.function_name for tc in model_response.tool_calls]
            logger.warning(
                "stop_check.stop_with_tool_calls",
                iteration_index=agent_state.iteration_count,
                tool_names=tool_names,
                hint="finish_reason=stop takes priority; tool_calls discarded",
            )
            # Clear tool_calls so they won't be dispatched
            model_response.tool_calls = []
            return StopSignal(reason=StopReason.LLM_STOP)

        if model_response.finish_reason == "length":
            logger.warning(
                "stop_check.output_truncated",
                iteration_index=agent_state.iteration_count,
                content_length=len(model_response.content or ""),
            )
            return StopSignal(
                reason=StopReason.OUTPUT_TRUNCATED,
                message="Model output was truncated due to length limit",
            )

        if agent_state.iteration_count + 1 >= agent.agent_config.max_iterations:
            logger.warning(
                "stop_check.max_iterations_reached",
                iteration_index=agent_state.iteration_count,
                max_iterations=agent.agent_config.max_iterations,
            )
            return StopSignal(
                reason=StopReason.MAX_ITERATIONS,
                message=f"Reached max iterations ({agent.agent_config.max_iterations})",
            )

        # Detect repeated identical tool calls — potential stuck loop.
        # Force stop if 3+ consecutive identical tool calls detected.
        if model_response.tool_calls and agent_state.iteration_history:
            repeat_count = self._detect_repeated_tool_calls(model_response, agent_state)
            if repeat_count >= _MAX_REPEATED_TOOL_CALLS:
                logger.warning(
                    "stop_check.stuck_loop_detected",
                    iteration_index=agent_state.iteration_count,
                    repeat_count=repeat_count,
                )
                return StopSignal(
                    reason=StopReason.ERROR,
                    message=f"Stuck loop: same tool called {repeat_count} times consecutively",
                )

        return None

    def _detect_repeated_tool_calls(
        self,
        model_response: ModelResponse,
        agent_state: AgentState,
    ) -> int:
        """Count consecutive identical tool call invocations.

        Returns the repeat count (including current). 1 = first occurrence.
        """
        current_sig = self._tool_call_signature(model_response.tool_calls)
        if not current_sig:
            return 0

        repeat_count = 1  # current call counts as 1
        for prev in reversed(agent_state.iteration_history):
            if prev.model_response and prev.model_response.tool_calls:
                prev_sig = self._tool_call_signature(prev.model_response.tool_calls)
                if prev_sig == current_sig:
                    repeat_count += 1
                else:
                    break
            else:
                break

        if repeat_count >= 2:
            logger.warning(
                "loop.repeated_tool_calls_detected",
                tool_signature=current_sig,
                repeat_count=repeat_count,
                iteration_index=agent_state.iteration_count,
                will_force_stop=repeat_count >= _MAX_REPEATED_TOOL_CALLS,
            )

        return repeat_count

    @staticmethod
    def _tool_call_signature(tool_calls: list[ToolCallRequest]) -> str:
        """Create a stable string signature for a set of tool calls."""
        import json
        parts = []
        for tc in sorted(tool_calls, key=lambda t: t.function_name):
            args_str = json.dumps(tc.arguments, sort_keys=True, default=str)
            parts.append(f"{tc.function_name}({args_str})")
        return "|".join(parts)

    @staticmethod
    def _single_tool_signature(tc: ToolCallRequest) -> str:
        """Signature for a single tool call (for dedup guard)."""
        import json
        args_str = json.dumps(tc.arguments, sort_keys=True, default=str)
        return f"{tc.function_name}({args_str})"

    def _collect_succeeded_signatures(self, agent_state: AgentState) -> set[str]:
        """Collect signatures of all tool calls that succeeded in previous iterations."""
        sigs: set[str] = set()
        for prev in agent_state.iteration_history:
            if not prev.model_response or not prev.model_response.tool_calls:
                continue
            # Build a map of tool_call_id -> success from tool_results
            success_ids = {
                tr.tool_call_id for tr in prev.tool_results if tr.success
            }
            for tc in prev.model_response.tool_calls:
                if tc.id in success_ids:
                    sigs.add(self._single_tool_signature(tc))
        return sigs

    async def _dispatch_tool_calls(
        self,
        agent: BaseAgent,
        tool_executor: ToolExecutorProtocol,
        tool_calls: list[ToolCallRequest],
        agent_state: AgentState,
    ) -> tuple[list[ToolResult], list[ToolExecutionMeta]]:
        """Dispatch tool calls with agent hook checks."""
        # Build set of previously-succeeded tool signatures for dedup guard
        succeeded_sigs = self._collect_succeeded_signatures(agent_state)

        capability_policy = agent.get_capability_policy()
        approved: list[ToolCallRequest] = []
        dedup_results: list[tuple[ToolResult, ToolExecutionMeta]] = []

        for tc in tool_calls:
            # Dedup guard: block re-execution of identical successful calls
            sig = self._single_tool_signature(tc)
            if sig in succeeded_sigs:
                logger.warning(
                    "tool.duplicate_blocked",
                    tool_name=tc.function_name,
                    reason="Identical call already succeeded in a previous iteration",
                )
                dedup_results.append((
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.function_name,
                        success=True,
                        output=(
                            "This tool was already called with identical arguments and succeeded. "
                            "Do NOT call it again. Summarize the previous result for the user."
                        ),
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                ))
                continue

            if hasattr(tool_executor, "is_tool_allowed"):
                if not tool_executor.is_tool_allowed(tc.function_name, capability_policy):
                    logger.warning(
                        "tool.blocked_by_capability_policy",
                        tool_name=tc.function_name,
                    )
                    # Return an error result so the LLM knows the tool was rejected
                    dedup_results.append((
                        ToolResult(
                            tool_call_id=tc.id,
                            tool_name=tc.function_name,
                            success=False,
                            output=(
                                f"Tool '{tc.function_name}' is not available. "
                                "Check available tools and try a different approach. "
                                "Do NOT retry this tool call."
                            ),
                        ),
                        ToolExecutionMeta(execution_time_ms=0, source="local"),
                    ))
                    continue
            decision = await agent.on_tool_call_requested(tc)
            if decision.allowed:
                approved.append(tc)
            else:
                logger.warning(
                    "tool.blocked_by_hook",
                    tool_name=tc.function_name,
                    reason=decision.reason,
                )
                # Return rejection feedback so the LLM knows why and doesn't retry
                dedup_results.append((
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.function_name,
                        success=False,
                        output=f"Tool call denied: {decision.reason or 'rejected by agent policy'}. Try a different approach.",
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                ))

        if not approved and not dedup_results:
            # All tool calls were silently blocked with no feedback — shouldn't happen
            # after the fixes above, but keep as safety net
            logger.warning(
                "tool.all_blocked",
                requested=[tc.function_name for tc in tool_calls],
            )
            return [], []

        results = []
        metas = []

        # Include dedup guard results first
        for result, meta in dedup_results:
            results.append(result)
            metas.append(meta)

        if not approved:
            return results, metas

        logger.info(
            "tool.batch_executing",
            tool_names=[tc.function_name for tc in approved],
            count=len(approved),
        )
        results_with_meta = await tool_executor.batch_execute(approved)

        for result, meta in results_with_meta:
            results.append(result)
            metas.append(meta)
            await agent.on_tool_call_completed(result)
            if result.success:
                output_preview = str(result.output)[:150] if result.output else ""
                logger.info(
                    "tool.completed",
                    tool_name=result.tool_name,
                    execution_time_ms=meta.execution_time_ms,
                    source=meta.source,
                    output_preview=output_preview,
                )
            else:
                logger.warning(
                    "tool.failed",
                    tool_name=result.tool_name,
                    execution_time_ms=meta.execution_time_ms,
                    source=meta.source,
                    error_type=result.error.error_type if result.error else "unknown",
                    error=str(result.error),
                )

        return results, metas

    def _handle_iteration_error(
        self,
        agent: BaseAgent,
        error: Exception,
        agent_state: AgentState,
        idx: int,
    ) -> IterationResult:
        """Handle iteration errors using agent's error policy."""
        strategy = agent.get_error_policy(error, agent_state)
        if strategy is None:
            strategy = ErrorStrategy.ABORT

        # Count consecutive errors for loop-safety detection
        consecutive_errors = 0
        for prev in reversed(agent_state.iteration_history):
            if prev.error:
                consecutive_errors += 1
            else:
                break

        logger.error(
            "llm.error",
            error_type=type(error).__name__,
            error=str(error),
            strategy=strategy.value,
            iteration_index=idx,
            consecutive_errors=consecutive_errors + 1,
            run_id=agent_state.run_id,
        )

        # Safety: force ABORT after too many consecutive errors to prevent retry loops
        if strategy != ErrorStrategy.ABORT and consecutive_errors + 1 >= _MAX_CONSECUTIVE_ERRORS:
            logger.warning(
                "llm.error.forced_abort",
                reason=f"Consecutive error count ({consecutive_errors + 1}) reached limit ({_MAX_CONSECUTIVE_ERRORS})",
                original_strategy=strategy.value,
                iteration_index=idx,
            )
            strategy = ErrorStrategy.ABORT

        if strategy == ErrorStrategy.ABORT:
            return IterationResult(
                iteration_index=idx,
                stop_signal=StopSignal(
                    reason=StopReason.ERROR,
                    message=f"LLM call failed: {error}",
                ),
                error=IterationError(
                    error_type=type(error).__name__,
                    error_message=str(error),
                    retryable=False,
                    stacktrace=traceback.format_exc(),
                ),
            )

        # SKIP or RETRY: return error but no stop signal
        logger.info(
            "llm.error.continuing",
            strategy=strategy.value,
            iteration_index=idx,
            hint="Will retry/skip — watch for consecutive_errors count",
        )
        return IterationResult(
            iteration_index=idx,
            error=IterationError(
                error_type=type(error).__name__,
                error_message=str(error),
                retryable=(strategy == ErrorStrategy.RETRY),
                stacktrace=traceback.format_exc(),
            ),
        )
