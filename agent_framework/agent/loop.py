from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.models.agent import (
    AgentState,
    AgentStatus,
    ErrorStrategy,
    IterationError,
    IterationResult,
    StopReason,
    StopSignal,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, ModelResponse, ToolCallRequest
from agent_framework.models.tool import ToolExecutionMeta, ToolResult

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps

logger = get_logger(__name__)


class AgentLoop:
    """Executes a single iteration of the agent loop.

    One iteration:
    1. Call LLM with prepared request
    2. Check stop conditions
    3. Dispatch tool calls if any
    4. Return structured IterationResult
    """

    async def execute_iteration(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        agent_state: AgentState,
        llm_request: LLMRequest,
    ) -> IterationResult:
        idx = agent_state.iteration_count

        await agent.on_iteration_started(idx, agent_state)
        logger.info("iteration.started", iteration_index=idx, run_id=agent_state.run_id)

        agent_state.status = AgentStatus.RUNNING

        # 1. Call LLM
        try:
            model_response = await self._call_llm(
                deps, llm_request.messages, llm_request.tools_schema, agent
            )
        except Exception as e:
            return self._handle_iteration_error(agent, e, agent_state, idx)

        # Update token usage
        agent_state.total_tokens_used += model_response.usage.total_tokens

        # 2. Check stop conditions
        stop_signal = self._check_stop_conditions(agent, model_response, agent_state)
        if stop_signal:
            logger.info(
                "iteration.completed",
                iteration_index=idx,
                stop_reason=stop_signal.reason.value,
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
            agent_state.status = AgentStatus.TOOL_CALLING
            tool_results, tool_metas = await self._dispatch_tool_calls(
                agent, deps, model_response.tool_calls, agent_state
            )

        logger.info("iteration.completed", iteration_index=idx)

        return IterationResult(
            iteration_index=idx,
            model_response=model_response,
            tool_results=tool_results,
            tool_execution_meta=tool_metas,
        )

    async def _call_llm(
        self,
        deps: AgentRuntimeDeps,
        messages: list[Message],
        tools_schema: list[dict],
        agent: BaseAgent,
    ) -> ModelResponse:
        logger.info("llm.called", model=agent.agent_config.model_name)
        response = await deps.model_adapter.complete(
            messages=messages,
            tools=tools_schema if tools_schema else None,
            temperature=agent.agent_config.temperature,
            max_tokens=agent.agent_config.max_output_tokens,
        )
        logger.info(
            "llm.responded",
            finish_reason=response.finish_reason,
            tool_calls_count=len(response.tool_calls),
            tokens=response.usage.total_tokens,
        )
        return response

    def _check_stop_conditions(
        self,
        agent: BaseAgent,
        model_response: ModelResponse,
        agent_state: AgentState,
    ) -> StopSignal | None:
        # LLM says stop and no tool calls
        if model_response.finish_reason == "stop" and not model_response.tool_calls:
            return StopSignal(reason=StopReason.LLM_STOP)

        # Output truncated
        if model_response.finish_reason == "length":
            return StopSignal(
                reason=StopReason.OUTPUT_TRUNCATED,
                message="Model output was truncated due to length limit",
            )

        # Max iterations
        if agent_state.iteration_count + 1 >= agent.agent_config.max_iterations:
            return StopSignal(
                reason=StopReason.MAX_ITERATIONS,
                message=f"Reached max iterations ({agent.agent_config.max_iterations})",
            )

        return None

    async def _dispatch_tool_calls(
        self,
        agent: BaseAgent,
        deps: AgentRuntimeDeps,
        tool_calls: list[ToolCallRequest],
        agent_state: AgentState,
    ) -> tuple[list[ToolResult], list[ToolExecutionMeta]]:
        """Dispatch tool calls with agent hook checks."""
        approved: list[ToolCallRequest] = []
        for tc in tool_calls:
            allowed = await agent.on_tool_call_requested(tc)
            if allowed:
                approved.append(tc)
            else:
                logger.warning("tool.blocked", tool_name=tc.function_name)

        if not approved:
            return [], []

        results_with_meta = await deps.tool_executor.batch_execute(approved)

        results = []
        metas = []
        for result, meta in results_with_meta:
            results.append(result)
            metas.append(meta)
            await agent.on_tool_call_completed(result)
            if result.success:
                logger.info("tool.completed", tool_name=result.tool_name)
            else:
                logger.warning(
                    "tool.failed",
                    tool_name=result.tool_name,
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

        logger.error(
            "llm.error",
            error=str(error),
            strategy=strategy.value,
            iteration_index=idx,
        )

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
        return IterationResult(
            iteration_index=idx,
            error=IterationError(
                error_type=type(error).__name__,
                error_message=str(error),
                retryable=(strategy == ErrorStrategy.RETRY),
                stacktrace=traceback.format_exc(),
            ),
        )
