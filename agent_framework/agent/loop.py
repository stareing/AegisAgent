from __future__ import annotations

import json as _json
import traceback
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.payloads import (iteration_error_payload,
                                            iteration_finish_payload,
                                            iteration_start_payload)
from agent_framework.infra.logger import get_logger
from agent_framework.infra.telemetry import get_tracing_manager
from agent_framework.models.agent import (AgentState, EffectiveRunConfig,
                                          ErrorStrategy, IterationError,
                                          IterationResult, StopReason,
                                          StopSignal)
from agent_framework.models.context import LLMRequest
from agent_framework.models.hook import HookPoint
from agent_framework.models.message import (ModelResponse, TokenUsage,
                                            ToolCallRequest)
from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.models.tool import ToolExecutionMeta, ToolResult

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.protocols.core import (ModelAdapterProtocol,
                                                ToolExecutorProtocol)

logger = get_logger(__name__)

# Safety: force stop if the same tool is called N+ times with identical args.
# Set to 2: first call executes, second repeat immediately stops.
# The dedup guard replays the cached result, so the LLM has the answer.
_MAX_REPEATED_TOOL_CALLS = 2
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
    hook_executor: Any = None


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
        _tm = get_tracing_manager()
        idx = agent_state.iteration_count
        llm_input_preview = self._summarize_llm_input(llm_request.messages)

        _iter_span = _tm.start_span("agent.iteration", attributes={
            "iteration_index": idx,
            "run_id": agent_state.run_id,
            "context_messages": len(llm_request.messages),
        })

        await agent.on_iteration_started(idx, agent_state)

        # ITERATION_START hook
        _hook_exec = getattr(loop_deps, 'hook_executor', None)
        _dispatcher = HookDispatchService(_hook_exec) if _hook_exec is not None else None
        if _dispatcher is not None:
            await _dispatcher.fire_advisory(
                HookPoint.ITERATION_START,
                run_id=agent_state.run_id, iteration_id=str(idx),
                payload=iteration_start_payload(idx, len(llm_request.messages)),
            )

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
            with _tm.span("agent.llm.call", attributes={
                "iteration_index": idx,
                "message_count": len(llm_request.messages),
            }):
                model_response = await self._call_llm(
                    loop_deps.model_adapter,
                    llm_request.messages,
                    llm_request.tools_schema,
                    effective_config,
                )
        except Exception as e:
            _iter_span.record_exception(e)
            _iter_span.end()
            return self._handle_iteration_error(agent, e, agent_state, idx, hook_executor=_hook_exec)

        self._enforce_tool_call_parallel_policy(model_response, effective_config, idx)

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
                llm_input_preview=llm_input_preview,
                model_response=model_response,
                stop_signal=stop_signal,
            )

        # 3. Dispatch tool calls
        tool_results: list[ToolResult] = []
        tool_metas: list[ToolExecutionMeta] = []

        if model_response.tool_calls:
            tool_names = [tc.function_name for tc in model_response.tool_calls]
            progressive = getattr(effective_config, "progressive_tool_results", False)
            logger.info(
                "iteration.dispatching_tools",
                iteration_index=idx,
                tool_count=len(model_response.tool_calls),
                tool_names=tool_names,
                mode="progressive" if progressive else "parallel",
            )
            tool_results, tool_metas = await self._dispatch_tool_calls(
                agent, loop_deps.tool_executor, model_response.tool_calls, agent_state,
                progressive=progressive,
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
            logger.warning(
                "iteration.no_tool_no_stop",
                iteration_index=idx,
                finish_reason=model_response.finish_reason,
                response_preview=(model_response.content or "")[:120],
            )

        _iter_span.set_attributes({
            "tokens_total": model_response.usage.total_tokens,
            "tool_count": len(tool_results),
            "finish_reason": model_response.finish_reason or "",
        })
        _iter_span.end()

        logger.info(
            "iteration.completed",
            iteration_index=idx,
            iteration_tokens=model_response.usage.total_tokens,
        )

        # ITERATION_FINISH hook
        if _dispatcher is not None:
            await _dispatcher.fire_advisory(
                HookPoint.ITERATION_FINISH,
                run_id=agent_state.run_id, iteration_id=str(idx),
                payload=iteration_finish_payload(
                    idx, len(tool_results), model_response.usage.total_tokens,
                ),
            )

        return IterationResult(
            iteration_index=idx,
            llm_input_preview=llm_input_preview,
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
                import inspect as _inspect
                delta = model_adapter.get_delta_messages(messages)
                if _inspect.isawaitable(delta):
                    delta.close()  # AsyncMock guard — discard coroutine
                    delta = messages
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

    async def _call_llm_stream(
        self,
        model_adapter: ModelAdapterProtocol,
        messages: list,
        tools_schema: list[dict],
        effective_config: EffectiveRunConfig,
    ) -> AsyncGenerator[StreamEvent | ModelResponse, None]:
        """Stream LLM response, yielding TOKEN events and finally the merged ModelResponse.

        The last yielded item is always a ModelResponse (not a StreamEvent).
        All prior items are StreamEvent(type=TOKEN).
        """
        actual_messages = messages
        is_delta = False
        if hasattr(model_adapter, "get_delta_messages") and hasattr(model_adapter, "_session"):
            try:
                import inspect as _inspect
                delta = model_adapter.get_delta_messages(messages)
                if _inspect.isawaitable(delta):
                    delta.close()
                    delta = messages
                if isinstance(delta, list) and len(delta) < len(messages):
                    is_delta = True
                    actual_messages = delta
            except Exception:
                pass

        logger.info(
            "llm.calling_stream",
            model=effective_config.model_name,
            temperature=effective_config.temperature,
            max_output_tokens=effective_config.max_output_tokens,
            message_count=len(actual_messages),
            full_message_count=len(messages),
            is_delta=is_delta,
            tools_count=len(tools_schema) if tools_schema else 0,
        )

        # Accumulate streamed chunks into a full response
        content_parts: list[str] = []
        tool_call_accum: dict[int, dict] = {}  # index -> {id, name, arguments_parts}
        finish_reason: str | None = None

        async for chunk in model_adapter.stream_complete(
            messages=actual_messages,
            tools=tools_schema if tools_schema else None,
            temperature=effective_config.temperature,
            max_tokens=effective_config.max_output_tokens,
        ):
            if chunk.delta_content:
                content_parts.append(chunk.delta_content)
                yield StreamEvent(
                    type=StreamEventType.TOKEN,
                    data={"text": chunk.delta_content},
                )

            if chunk.delta_tool_calls:
                for tc_delta in chunk.delta_tool_calls:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_call_accum:
                        tool_call_accum[idx] = {
                            "id": tc_delta.get("id", ""),
                            "name": "",
                            "arguments_parts": [],
                        }
                    accum = tool_call_accum[idx]
                    if tc_delta.get("id"):
                        accum["id"] = tc_delta["id"]
                    func = tc_delta.get("function", {})
                    if func.get("name"):
                        accum["name"] = func["name"]
                    if func.get("arguments"):
                        accum["arguments_parts"].append(func["arguments"])

            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        # Merge accumulated chunks into ModelResponse
        content = "".join(content_parts) or None
        tool_calls: list[ToolCallRequest] = []
        for _idx in sorted(tool_call_accum):
            accum = tool_call_accum[_idx]
            args_str = "".join(accum["arguments_parts"]) or "{}"
            try:
                arguments = _json.loads(args_str)
            except (ValueError, _json.JSONDecodeError) as parse_err:
                logger.warning(
                    "llm.tool_call_parse_error",
                    tool_name=accum["name"],
                    raw_arguments=args_str[:200],
                    error=str(parse_err),
                )
                arguments = {"_parse_error": str(parse_err), "_raw": args_str[:500]}
            tool_calls.append(ToolCallRequest(
                id=accum["id"],
                function_name=accum["name"],
                arguments=arguments,
            ))

        if not finish_reason:
            finish_reason = "tool_calls" if tool_calls else "stop"
        if finish_reason not in ("stop", "tool_calls", "length", "error"):
            finish_reason = "stop"

        response = ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=TokenUsage(),
        )
        logger.info(
            "llm.stream_completed",
            finish_reason=finish_reason,
            tool_calls_count=len(tool_calls),
            content_length=len(content) if content else 0,
        )
        yield response

    async def execute_iteration_stream(
        self,
        agent: BaseAgent,
        loop_deps: AgentLoopDeps,
        agent_state: AgentState,
        llm_request: LLMRequest,
        effective_config: EffectiveRunConfig,
    ) -> AsyncGenerator[StreamEvent | IterationResult, None]:
        """Streaming variant of execute_iteration.

        Yields StreamEvents during LLM call and tool execution.
        The last yielded item is always the IterationResult.
        """
        idx = agent_state.iteration_count
        llm_input_preview = self._summarize_llm_input(llm_request.messages)
        await agent.on_iteration_started(idx, agent_state)

        # ITERATION_START hook
        _hook_exec = getattr(loop_deps, 'hook_executor', None)
        _dispatcher = HookDispatchService(_hook_exec) if _hook_exec is not None else None
        if _dispatcher is not None:
            await _dispatcher.fire_advisory(
                HookPoint.ITERATION_START,
                run_id=agent_state.run_id, iteration_id=str(idx),
                payload=iteration_start_payload(idx, len(llm_request.messages)),
            )

        yield StreamEvent(
            type=StreamEventType.ITERATION_START,
            data={"iteration_index": idx},
        )

        # 1. Call LLM with streaming
        model_response: ModelResponse | None = None
        try:
            async for item in self._call_llm_stream(
                loop_deps.model_adapter,
                llm_request.messages,
                llm_request.tools_schema,
                effective_config,
            ):
                if isinstance(item, ModelResponse):
                    model_response = item
                else:
                    yield item  # StreamEvent(TOKEN)
        except Exception as e:
            yield self._handle_iteration_error(agent, e, agent_state, idx, hook_executor=_hook_exec)
            return

        assert model_response is not None
        self._enforce_tool_call_parallel_policy(model_response, effective_config, idx)

        # 2. Check stop conditions
        stop_signal = self._check_stop_conditions(agent, model_response, agent_state)
        if stop_signal:
            yield IterationResult(
                iteration_index=idx,
                llm_input_preview=llm_input_preview,
                model_response=model_response,
                stop_signal=stop_signal,
            )
            return

        if model_response.tool_calls:
            yield StreamEvent(
                type=StreamEventType.ASSISTANT_TOOL_CALLS,
                data={
                    "content": model_response.content,
                    "tool_calls": model_response.tool_calls,
                },
            )

        # 3. Dispatch tool calls
        tool_results: list[ToolResult] = []
        tool_metas: list[ToolExecutionMeta] = []

        if model_response.tool_calls:
            progressive = getattr(effective_config, "progressive_tool_results", False)
            total_tools = len(model_response.tool_calls)
            is_progressive = (
                progressive
                and total_tools > 1
                and hasattr(type(loop_deps.tool_executor), "batch_execute_progressive")
            )

            # Emit start events — PROGRESSIVE_START for all tools in progressive mode
            for ti, tc in enumerate(model_response.tool_calls):
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_START,
                    data={
                        "tool_name": tc.function_name,
                        "tool_call_id": tc.id,
                        "arguments": tc.arguments,
                    },
                )
                if is_progressive:
                    description = self._progressive_tool_description(tc)
                    yield StreamEvent(
                        type=StreamEventType.PROGRESSIVE_START,
                        data={"tool_call_id": tc.id, "tool_name": tc.function_name,
                              "description": description,
                              "index": ti + 1, "total": total_tools},
                    )

            if is_progressive:
                # Progressive: yield events AS EACH TOOL COMPLETES — true real-time
                completed = 0
                async for result, meta in self._dispatch_progressive_stream(
                    agent, loop_deps.tool_executor, model_response.tool_calls, agent_state,
                ):
                    completed += 1
                    tool_results.append(result)
                    tool_metas.append(meta)

                    # Full output string — no truncation for downstream consumers
                    raw_output = result.output
                    output_str = str(raw_output) if raw_output else ""

                    # Human-readable display_text: for delegation results extract
                    # the summary field; for other tools use the raw output.
                    if isinstance(raw_output, dict) and "summary" in raw_output:
                        display_text = str(raw_output["summary"])
                    else:
                        display_text = output_str

                    # TOOL_CALL_DONE — immediate, full output
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DONE,
                        data={
                            "tool_name": result.tool_name,
                            "tool_call_id": result.tool_call_id,
                            "success": result.success,
                            "output": output_str,
                        },
                    )

                    # PROGRESSIVE_DONE — immediate, with display_text for UI
                    description = ""
                    for tc in model_response.tool_calls:
                        if tc.id == result.tool_call_id:
                            description = self._progressive_tool_description(tc)
                            break
                    yield StreamEvent(
                        type=StreamEventType.PROGRESSIVE_DONE,
                        data={
                            "tool_call_id": result.tool_call_id,
                            "tool_name": result.tool_name,
                            "description": description,
                            "success": result.success,
                            "output": output_str,
                            "display_text": display_text,
                            "index": completed,
                            "total": total_tools,
                        },
                    )
            else:
                # Non-progressive: batch execute, then emit all DONE events
                tool_results, tool_metas = await self._dispatch_tool_calls(
                    agent, loop_deps.tool_executor, model_response.tool_calls, agent_state,
                )
                for tr, tm in zip(tool_results, tool_metas):
                    output_str = str(tr.output)[:500] if tr.output else ""
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DONE,
                        data={
                            "tool_name": tr.tool_name,
                            "tool_call_id": tr.tool_call_id,
                            "success": tr.success,
                            "output": output_str,
                        },
                    )

        # ITERATION_FINISH hook
        if _dispatcher is not None:
            await _dispatcher.fire_advisory(
                HookPoint.ITERATION_FINISH,
                run_id=agent_state.run_id, iteration_id=str(idx),
                payload=iteration_finish_payload(
                    idx, len(tool_results), model_response.usage.total_tokens,
                ),
            )

        yield IterationResult(
            iteration_index=idx,
            llm_input_preview=llm_input_preview,
            model_response=model_response,
            tool_results=tool_results,
            tool_execution_meta=tool_metas,
        )

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
        # Prioritize tool_calls — the model produced actionable calls despite
        # the misleading finish_reason. Treat as tool_calls finish_reason.
        if model_response.finish_reason == "stop" and model_response.tool_calls:
            tool_names = [tc.function_name for tc in model_response.tool_calls]
            logger.warning(
                "stop_check.stop_with_tool_calls",
                iteration_index=agent_state.iteration_count,
                tool_names=tool_names,
                hint="tool_calls present; overriding finish_reason to tool_calls",
            )
            model_response.finish_reason = "tool_calls"
            return None  # Do not stop — let tool dispatch proceed

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

        max_iterations = agent.agent_config.max_iterations
        if max_iterations > 0 and agent_state.iteration_count + 1 >= max_iterations:
            logger.warning(
                "stop_check.max_iterations_reached",
                iteration_index=agent_state.iteration_count,
                max_iterations=max_iterations,
            )
            return StopSignal(
                reason=StopReason.MAX_ITERATIONS,
                message=f"Reached max iterations ({max_iterations})",
            )

        # Detect repeated identical tool calls — potential stuck loop.
        # Stop early and use the previous iteration's answer if available.
        if model_response.tool_calls and agent_state.iteration_history:
            repeat_count = self._detect_repeated_tool_calls(model_response, agent_state)
            if repeat_count >= _MAX_REPEATED_TOOL_CALLS:
                logger.warning(
                    "stop_check.stuck_loop_detected",
                    iteration_index=agent_state.iteration_count,
                    repeat_count=repeat_count,
                )
                # Extract previous answer: check content first, then tool results
                prev = agent_state.iteration_history[-1] if agent_state.iteration_history else None
                prev_answer = None
                if prev:
                    prev_content = prev.model_response.content if prev.model_response else None
                    prev_tool_count = len(prev.tool_results)
                    prev_success = [tr for tr in prev.tool_results if tr.success and tr.output]
                    logger.info(
                        "stop_check.extracting_prev_answer",
                        has_prev=True,
                        content_preview=repr(prev_content)[:50],
                        tool_results_count=prev_tool_count,
                        successful_count=len(prev_success),
                    )
                    if prev_content:
                        prev_answer = prev_content
                    elif prev_success:
                        prev_answer = str(prev_success[0].output)

                if prev_answer:
                    model_response.content = prev_answer
                    model_response.tool_calls = []
                    return StopSignal(
                        reason=StopReason.LLM_STOP,
                        message="Stopped repeated tool call — using previous result",
                    )
                return StopSignal(
                    reason=StopReason.MAX_ITERATIONS,
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

    def _enforce_tool_call_parallel_policy(
        self,
        model_response: ModelResponse,
        effective_config: EffectiveRunConfig,
        iteration_index: int,
    ) -> None:
        """Apply runtime parallel-call policy as a hard execution guard."""
        if (
            not effective_config.allow_parallel_tool_calls
            and model_response.tool_calls
            and len(model_response.tool_calls) > 1
        ):
            dropped = model_response.tool_calls[1:]
            model_response.tool_calls = model_response.tool_calls[:1]
            logger.warning(
                "tool.parallel_calls_trimmed",
                iteration_index=iteration_index,
                kept_tool=model_response.tool_calls[0].function_name,
                dropped_tools=[tc.function_name for tc in dropped],
                dropped_count=len(dropped),
            )

    @staticmethod
    def _summarize_llm_input(messages: list[Any]) -> str:
        """Build a concise, deterministic preview of model input messages."""
        if not messages:
            return "(empty)"

        tail = messages[-6:]
        lines: list[str] = []
        for m in tail:
            role = str(getattr(m, "role", "unknown"))
            content = str(getattr(m, "content", "") or "")
            tool_calls = getattr(m, "tool_calls", None)
            tool_call_id = getattr(m, "tool_call_id", None)
            name = getattr(m, "name", None)

            if content and len(content) > 180:
                content = content[:180] + "... [truncated]"

            extra = []
            if name:
                extra.append(f"name={name}")
            if tool_call_id:
                extra.append(f"tool_call_id={tool_call_id}")
            if tool_calls:
                try:
                    tc_names = [tc.function_name for tc in tool_calls]
                except Exception:
                    tc_names = ["<unknown>"]
                extra.append(f"tool_calls={','.join(tc_names)}")

            suffix = f" ({'; '.join(extra)})" if extra else ""
            lines.append(f"[{role}]{suffix} {content}".rstrip())

        if len(messages) > len(tail):
            lines.insert(0, f"... {len(messages) - len(tail)} earlier messages omitted ...")
        return "\n".join(lines)

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

    def _collect_succeeded_signatures(
        self, agent_state: AgentState
    ) -> dict[str, str]:
        """Collect signatures → original output of succeeded tool calls.

        Returns a dict mapping signature → original output string,
        so dedup guard can replay the real result instead of an instruction.
        """
        sigs: dict[str, str] = {}
        for prev in agent_state.iteration_history:
            if not prev.model_response or not prev.model_response.tool_calls:
                continue
            # Build map of tool_call_id -> output from successful results
            success_outputs: dict[str, str] = {}
            for tr in prev.tool_results:
                if tr.success and tr.output is not None:
                    success_outputs[tr.tool_call_id] = str(tr.output)
            for tc in prev.model_response.tool_calls:
                if tc.id in success_outputs:
                    sig = self._single_tool_signature(tc)
                    if sig not in sigs:  # keep first occurrence
                        sigs[sig] = success_outputs[tc.id]
        return sigs

    async def _dispatch_tool_calls(
        self,
        agent: BaseAgent,
        tool_executor: ToolExecutorProtocol,
        tool_calls: list[ToolCallRequest],
        agent_state: AgentState,
        progressive: bool = False,
    ) -> tuple[list[ToolResult], list[ToolExecutionMeta]]:
        """Dispatch tool calls with pre-dispatch constraints.

        Three-layer pre-dispatch guard (before any execution):
        1. Cross-iteration dedup: same tool+args succeeded before → replay cached result
        2. Intra-batch dedup: same tool+args appears multiple times in this batch → keep first only
        3. Capability policy + agent hook: is this tool allowed?

        After guards: different tools may execute in parallel.
        Same tool with different args: allowed (different operations).
        Same tool with same args: blocked (duplicate, cached result returned).
        """
        # Layer 1: Cross-iteration dedup — collect previously succeeded signatures
        succeeded_sigs = self._collect_succeeded_signatures(agent_state)

        capability_policy = agent.get_capability_policy()
        approved: list[ToolCallRequest] = []
        pre_results: list[tuple[ToolResult, ToolExecutionMeta]] = []

        # Layer 2: Intra-batch dedup — track signatures within this batch
        batch_seen_sigs: set[str] = set()

        for tc in tool_calls:
            sig = self._single_tool_signature(tc)

            # Guard 1: Cross-iteration dedup
            if sig in succeeded_sigs:
                original_output = succeeded_sigs[sig]
                logger.info(
                    "tool.cross_iter_dedup",
                    tool_name=tc.function_name,
                    action="replay_cached",
                )
                pre_results.append((
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.function_name,
                        success=True,
                        output=original_output,
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                ))
                continue

            # Guard 2: Intra-batch dedup (same tool+args in this response)
            if sig in batch_seen_sigs:
                logger.info(
                    "tool.intra_batch_dedup",
                    tool_name=tc.function_name,
                    action="skip_duplicate_in_batch",
                )
                pre_results.append((
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.function_name,
                        success=False,
                        output=f"Duplicate call in same batch — '{tc.function_name}' already queued with identical arguments.",
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                ))
                continue
            batch_seen_sigs.add(sig)

            if hasattr(tool_executor, "is_tool_allowed"):
                if not tool_executor.is_tool_allowed(tc.function_name, capability_policy):
                    logger.warning(
                        "tool.blocked_by_capability_policy",
                        tool_name=tc.function_name,
                    )
                    # Return an error result so the LLM knows the tool was rejected
                    pre_results.append((
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
                pre_results.append((
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.function_name,
                        success=False,
                        output=f"Tool call denied: {decision.reason or 'rejected by agent policy'}. Try a different approach.",
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                ))

        if not approved and not pre_results:
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
        for result, meta in pre_results:
            results.append(result)
            metas.append(meta)

        if not approved:
            return results, metas

        logger.info(
            "tool.batch_executing",
            tool_names=[tc.function_name for tc in approved],
            count=len(approved),
            mode="progressive" if progressive else "parallel",
        )

        if progressive and len(approved) > 1 and hasattr(type(tool_executor), "batch_execute_progressive"):
            # Progressive: all tools run in parallel, results stream back
            # as each completes. The LLM sees tool_result messages arriving
            # one by one in completion order within a single iteration.
            async for result, meta in tool_executor.batch_execute_progressive(approved):
                results.append(result)
                metas.append(meta)
                await agent.on_tool_call_completed(result)
                logger.info(
                    "tool.progressive_completed",
                    tool_name=result.tool_name,
                    success=result.success,
                    execution_time_ms=meta.execution_time_ms,
                    completed_so_far=len(results),
                    total=len(approved) + len(pre_results),
                )
        else:
            # Parallel (default) or single tool: execute all together
            results_with_meta = await tool_executor.batch_execute(approved)
            for result, meta in results_with_meta:
                results.append(result)
                metas.append(meta)
                await agent.on_tool_call_completed(result)

        for result, meta in zip(results[-len(approved):], metas[-len(approved):]):
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

    async def _dispatch_progressive_stream(
        self,
        agent: BaseAgent,
        tool_executor: ToolExecutorProtocol,
        tool_calls: list[ToolCallRequest],
        agent_state: AgentState,
    ) -> AsyncGenerator[tuple[ToolResult, ToolExecutionMeta], None]:
        """Progressive tool dispatch — yields (result, meta) as each completes.

        Runs pre-dispatch guards (dedup, policy, hooks) identically to
        _dispatch_tool_calls, then uses batch_execute_progressive to yield
        results in completion order. Each yield triggers an immediate
        TOOL_CALL_DONE event in the caller.
        """
        succeeded_sigs = self._collect_succeeded_signatures(agent_state)
        capability_policy = agent.get_capability_policy()
        approved: list[ToolCallRequest] = []

        for tc in tool_calls:
            sig = self._single_tool_signature(tc)
            if sig in succeeded_sigs:
                yield (
                    ToolResult(
                        tool_call_id=tc.id, tool_name=tc.function_name,
                        success=True, output=succeeded_sigs[sig],
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                )
                continue
            if hasattr(tool_executor, "is_tool_allowed"):
                if not tool_executor.is_tool_allowed(tc.function_name, capability_policy):
                    yield (
                        ToolResult(
                            tool_call_id=tc.id, tool_name=tc.function_name,
                            success=False, output=f"Tool '{tc.function_name}' blocked by policy.",
                        ),
                        ToolExecutionMeta(execution_time_ms=0, source="local"),
                    )
                    continue
            decision = await agent.on_tool_call_requested(tc)
            if decision.allowed:
                approved.append(tc)
            else:
                yield (
                    ToolResult(
                        tool_call_id=tc.id, tool_name=tc.function_name,
                        success=False, output=f"Tool denied: {decision.reason or 'policy'}",
                    ),
                    ToolExecutionMeta(execution_time_ms=0, source="local"),
                )

        if approved:
            async for result, meta in tool_executor.batch_execute_progressive(approved):
                await agent.on_tool_call_completed(result)
                logger.info(
                    "tool.progressive_done",
                    tool_name=result.tool_name,
                    success=result.success,
                    execution_time_ms=meta.execution_time_ms,
                )
                yield result, meta

    @staticmethod
    def _progressive_tool_description(tc: ToolCallRequest) -> str:
        """Extract a human-readable description for progressive UI display."""
        if tc.function_name == "spawn_agent":
            return str(tc.arguments.get("task_input", ""))[:100]
        # For other tools: tool_name + key argument summary
        args = tc.arguments or {}
        if not args:
            return tc.function_name
        # Pick the first string-valued argument as summary
        for key in ("query", "input", "text", "command", "path", "url", "name", "task"):
            if key in args:
                return f"{tc.function_name}({str(args[key])[:60]})"
        # Fallback: first arg value
        first_key = next(iter(args))
        return f"{tc.function_name}({first_key}={str(args[first_key])[:40]})"

    def _handle_iteration_error(
        self,
        agent: BaseAgent,
        error: Exception,
        agent_state: AgentState,
        idx: int,
        hook_executor: Any = None,
    ) -> IterationResult:
        """Handle iteration errors using agent's error policy."""
        # ITERATION_ERROR hook
        if hook_executor is not None:
            try:
                _err_disp = HookDispatchService(hook_executor)
                import asyncio
                asyncio.get_event_loop().create_task(
                    _err_disp.fire_advisory(
                        HookPoint.ITERATION_ERROR,
                        run_id=agent_state.run_id, iteration_id=str(idx),
                        payload=iteration_error_payload(
                            idx, type(error).__name__, str(error),
                        ),
                    )
                )
            except Exception:
                pass

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
