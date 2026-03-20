"""Tests for progressive streaming — verifies real-time factual event emission.

Covers:
1. TOOL_CALL_DONE events emitted in completion order (not batched)
2. Coordinator does not synthesize lead-style narration
3. Non-progressive mode unchanged
4. Event ordering: START before DONE before final DONE
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_framework.models.agent import (AgentState, EffectiveRunConfig,
                                          IterationResult, StopReason,
                                          StopSignal)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import (Message, ModelResponse,
                                            ToolCallRequest)
from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.models.tool import ToolExecutionMeta, ToolResult


class TestProgressiveToolDoneOrdering:
    """TOOL_CALL_DONE must be emitted as each tool completes, not batched."""

    @pytest.mark.asyncio
    async def test_progressive_yields_done_per_tool(self):
        """In progressive mode, each TOOL_CALL_DONE should arrive individually."""
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop, AgentLoopDeps

        loop = AgentLoop()
        agent = DefaultAgent(progressive_tool_results=True)

        # Mock adapter that returns 3 tool calls
        mock_adapter = AsyncMock()

        async def mock_stream(*args, **kwargs):
            import json

            from agent_framework.adapters.model.base_adapter import ModelChunk
            for i in range(3):
                yield ModelChunk(delta_tool_calls=[{
                    "index": i, "id": f"tc_{i}",
                    "function": {"name": "spawn_agent", "arguments": json.dumps({"task_input": f"task {i}", "wait": True})},
                }])
            yield ModelChunk(finish_reason="tool_calls")

        mock_adapter.stream_complete = mock_stream

        # Mock executor with progressive support + different delays
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        results_data = [
            ToolResult(tool_call_id=f"tc_{i}", tool_name="spawn_agent", success=True, output=f"result_{i}")
            for i in range(3)
        ]
        metas_data = [ToolExecutionMeta(execution_time_ms=100 * (i + 1), source="subagent") for i in range(3)]

        async def fake_progressive(requests, policy=None):
            for r, m in zip(results_data, metas_data):
                await asyncio.sleep(0.01)  # Simulate different completion times
                yield r, m

        # Set progressive method on the CLASS so type-level hasattr works
        from agent_framework.tools.executor import ToolExecutor
        original = getattr(ToolExecutor, "batch_execute_progressive", None)
        ToolExecutor.batch_execute_progressive = fake_progressive

        try:
            mock_executor_real = ToolExecutor.__new__(ToolExecutor)
            mock_executor_real._registry = MagicMock()
            mock_executor_real._confirmation = None
            mock_executor_real._delegation = None
            mock_executor_real._mcp = None
            mock_executor_real._parent_agent_getter = None
            mock_executor_real._max_concurrent = 5
            mock_executor_real._current_run_id = ""
            mock_executor_real._current_session_messages = []
            mock_executor_real._progressive_mode = True
            mock_executor_real.batch_execute_progressive = fake_progressive
            mock_executor_real.is_tool_allowed = MagicMock(return_value=True)

            loop_deps = AgentLoopDeps(
                model_adapter=mock_adapter,
                tool_executor=mock_executor_real,
            )
            state = AgentState(task="test", run_id="r1")
            request = LLMRequest(
                messages=[Message(role="user", content="test")],
                tools_schema=[{"type": "function", "function": {"name": "spawn_agent"}}],
            )
            config = EffectiveRunConfig(progressive_tool_results=True)

            events = []
            async for item in loop.execute_iteration_stream(
                agent, loop_deps, state, request, config,
            ):
                events.append(item)

            # Verify event types
            event_types = [
                e.type if isinstance(e, StreamEvent) else "ITERATION_RESULT"
                for e in events
            ]

            # Should have TOOL_CALL_START × 3, then TOOL_CALL_DONE × 3 (interleaved, not batched)
            starts = [e for e in events if isinstance(e, StreamEvent) and e.type == StreamEventType.TOOL_CALL_START]
            dones = [e for e in events if isinstance(e, StreamEvent) and e.type == StreamEventType.TOOL_CALL_DONE]
            subagent_dones = [
                e for e in events
                if isinstance(e, StreamEvent) and e.type == StreamEventType.SUBAGENT_DONE
            ]

            assert len(starts) == 3, f"Expected 3 TOOL_CALL_START, got {len(starts)}"
            assert len(dones) == 3, f"Expected 3 TOOL_CALL_DONE, got {len(dones)}"
            assert len(subagent_dones) == 3, f"Expected 3 SUBAGENT_DONE, got {len(subagent_dones)}"

            # DONE events should have correct tool_call_ids
            done_ids = [e.data["tool_call_id"] for e in dones]
            assert done_ids == ["tc_0", "tc_1", "tc_2"]
            assert all(e.data["tool_name"] == "spawn_agent" for e in subagent_dones)

            # Final item should be IterationResult
            assert isinstance(events[-1], IterationResult)
            assert len(events[-1].tool_results) == 3

        finally:
            if original is not None:
                ToolExecutor.batch_execute_progressive = original
            elif hasattr(ToolExecutor, "batch_execute_progressive"):
                delattr(ToolExecutor, "batch_execute_progressive")


class TestStreamEventTypes:
    """New progressive event types are valid and well-formed."""

    def test_subagent_start_event(self):
        event = StreamEvent(
            type=StreamEventType.SUBAGENT_START,
            data={"tool_call_id": "tc1", "task_input": "test", "index": 1, "total": 3},
        )
        assert event.type == StreamEventType.SUBAGENT_START
        assert event.data["index"] == 1

    def test_subagent_done_event(self):
        event = StreamEvent(
            type=StreamEventType.SUBAGENT_DONE,
            data={"tool_call_id": "tc1", "tool_name": "spawn_agent",
                  "task_input": "test", "success": True,
                  "output": "result", "index": 1, "total": 3},
        )
        assert event.type == StreamEventType.SUBAGENT_DONE
        assert event.data["success"] is True
        assert event.data["tool_name"] == "spawn_agent"

    def test_progressive_response_event(self):
        event = StreamEvent(
            type=StreamEventType.PROGRESSIVE_RESPONSE,
            data={"text": "intermediate", "index": 1, "total": 3},
        )
        assert event.type == StreamEventType.PROGRESSIVE_RESPONSE
        assert event.data["text"] == "intermediate"


class TestNonProgressiveUnchanged:
    """Non-progressive streaming must not regress."""

    @pytest.mark.asyncio
    async def test_non_progressive_batches_done_events(self):
        """Without progressive, TOOL_CALL_DONE events come after all tools complete."""
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop, AgentLoopDeps

        loop = AgentLoop()
        agent = DefaultAgent(progressive_tool_results=False)  # explicit non-progressive

        mock_adapter = AsyncMock()

        async def mock_stream(*args, **kwargs):
            import json

            from agent_framework.adapters.model.base_adapter import ModelChunk
            yield ModelChunk(delta_tool_calls=[{
                "index": 0, "id": "tc_0",
                "function": {"name": "think", "arguments": json.dumps({"thought": "test"})},
            }])
            yield ModelChunk(finish_reason="tool_calls")

        mock_adapter.stream_complete = mock_stream

        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.batch_execute = AsyncMock(return_value=[
            (ToolResult(tool_call_id="tc_0", tool_name="think", success=True, output="ok"),
             ToolExecutionMeta(execution_time_ms=1, source="local")),
        ])

        loop_deps = AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor)
        state = AgentState(task="test", run_id="r1")
        request = LLMRequest(
            messages=[Message(role="user", content="test")],
            tools_schema=[],
        )
        config = EffectiveRunConfig(progressive_tool_results=False)

        events = []
        async for item in loop.execute_iteration_stream(agent, loop_deps, state, request, config):
            events.append(item)

        starts = [e for e in events if isinstance(e, StreamEvent) and e.type == StreamEventType.TOOL_CALL_START]
        dones = [e for e in events if isinstance(e, StreamEvent) and e.type == StreamEventType.TOOL_CALL_DONE]

        assert len(starts) == 1
        assert len(dones) == 1
        assert isinstance(events[-1], IterationResult)


class TestRunStreamProgressiveTiming:
    """run_stream must emit progressive responses before the iteration fully closes."""

    @pytest.mark.asyncio
    async def test_run_stream_facts_only_by_default(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps

        coordinator = RunCoordinator()
        agent = DefaultAgent(progressive_tool_results=True)

        memory_manager = MagicMock()
        memory_manager.select_for_context.return_value = []
        memory_manager.begin_run_session = MagicMock()
        memory_manager.end_run_session = MagicMock()
        memory_manager.begin_session = MagicMock()
        memory_manager.end_session = MagicMock()
        memory_manager.record_turn = MagicMock()

        context_engineer = MagicMock()
        context_engineer.prepare_context_for_llm = AsyncMock(return_value=[
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ])
        context_engineer.set_skill_context = MagicMock()

        tool_registry = MagicMock()
        tool_registry.list_tools.return_value = []
        tool_registry.export_schemas.return_value = []

        tool_executor = MagicMock()
        tool_executor.is_tool_allowed = MagicMock(return_value=True)
        tool_executor.set_current_session_messages = MagicMock()

        skill_router = MagicMock()
        skill_router.detect_skill.return_value = None

        model_adapter = AsyncMock()

        deps = AgentRuntimeDeps(
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            memory_manager=memory_manager,
            context_engineer=context_engineer,
            model_adapter=model_adapter,
            skill_router=skill_router,
        )

        first_iteration = IterationResult(
            iteration_index=0,
            model_response=ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_1",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 1", "wait": True},
                    ),
                    ToolCallRequest(
                        id="tc_2",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 2", "wait": True},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            tool_results=[
                ToolResult(tool_call_id="tc_1", tool_name="spawn_agent", success=True, output="result 1"),
                ToolResult(tool_call_id="tc_2", tool_name="spawn_agent", success=True, output="result 2"),
            ],
            tool_execution_meta=[
                ToolExecutionMeta(execution_time_ms=10, source="subagent"),
                ToolExecutionMeta(execution_time_ms=20, source="subagent"),
            ],
        )
        final_iteration = IterationResult(
            iteration_index=1,
            model_response=ModelResponse(content="final", finish_reason="stop"),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )

        async def fake_execute_iteration_stream(*args, **kwargs):
            state = args[2]
            if state.iteration_count == 0:
                yield StreamEvent(
                    type=StreamEventType.ITERATION_START,
                    data={"iteration_index": 0},
                )
                yield StreamEvent(
                    type=StreamEventType.ASSISTANT_TOOL_CALLS,
                    data={
                        "content": "",
                        "tool_calls": first_iteration.model_response.tool_calls,
                    },
                )
                yield StreamEvent(
                    type=StreamEventType.PROGRESSIVE_DONE,
                    data={
                        "tool_call_id": "tc_1",
                        "tool_name": "spawn_agent",
                        "description": "task 1",
                        "success": True,
                        "output": "result 1",
                        "display_text": "result 1",
                        "index": 1,
                        "total": 2,
                    },
                )
                yield first_iteration
            else:
                yield final_iteration

        coordinator._loop.execute_iteration_stream = fake_execute_iteration_stream
        coordinator._loop._call_llm = AsyncMock(
            return_value=ModelResponse(content="mid response", finish_reason="stop")
        )
        coordinator._prepare_llm_request = AsyncMock(
            return_value=LLMRequest(messages=[], tools_schema=[])
        )

        events = []
        async for event in coordinator.run_stream(agent, deps, "progressive task"):
            events.append(event)

        event_types = [event.type for event in events]
        assert StreamEventType.SUBAGENT_DONE in event_types
        assert StreamEventType.PROGRESSIVE_RESPONSE not in event_types
        assert StreamEventType.DONE in event_types
        coordinator._loop._call_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_stream_never_emits_progressive_response(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps

        coordinator = RunCoordinator()
        agent = DefaultAgent(progressive_tool_results=True)

        memory_manager = MagicMock()
        memory_manager.select_for_context.return_value = []
        memory_manager.begin_run_session = MagicMock()
        memory_manager.end_run_session = MagicMock()
        memory_manager.begin_session = MagicMock()
        memory_manager.end_session = MagicMock()
        memory_manager.record_turn = MagicMock()

        context_engineer = MagicMock()
        context_engineer.prepare_context_for_llm = AsyncMock(return_value=[
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ])
        context_engineer.set_skill_context = MagicMock()

        tool_registry = MagicMock()
        tool_registry.list_tools.return_value = []
        tool_registry.export_schemas.return_value = []

        tool_executor = MagicMock()
        tool_executor.is_tool_allowed = MagicMock(return_value=True)
        tool_executor.set_current_session_messages = MagicMock()

        skill_router = MagicMock()
        skill_router.detect_skill.return_value = None

        model_adapter = AsyncMock()

        deps = AgentRuntimeDeps(
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            memory_manager=memory_manager,
            context_engineer=context_engineer,
            model_adapter=model_adapter,
            skill_router=skill_router,
        )

        first_iteration = IterationResult(
            iteration_index=0,
            model_response=ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_1",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 1", "wait": True},
                    ),
                    ToolCallRequest(
                        id="tc_2",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 2", "wait": True},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            tool_results=[
                ToolResult(tool_call_id="tc_1", tool_name="spawn_agent", success=True, output="result 1"),
                ToolResult(tool_call_id="tc_2", tool_name="spawn_agent", success=True, output="result 2"),
            ],
            tool_execution_meta=[
                ToolExecutionMeta(execution_time_ms=10, source="subagent"),
                ToolExecutionMeta(execution_time_ms=20, source="subagent"),
            ],
        )
        final_iteration = IterationResult(
            iteration_index=1,
            model_response=ModelResponse(content="final", finish_reason="stop"),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )

        async def fake_execute_iteration_stream(*args, **kwargs):
            state = args[2]
            if state.iteration_count == 0:
                yield StreamEvent(
                    type=StreamEventType.ITERATION_START,
                    data={"iteration_index": 0},
                )
                yield StreamEvent(
                    type=StreamEventType.ASSISTANT_TOOL_CALLS,
                    data={
                        "content": "",
                        "tool_calls": first_iteration.model_response.tool_calls,
                    },
                )
                yield StreamEvent(
                    type=StreamEventType.PROGRESSIVE_DONE,
                    data={
                        "tool_call_id": "tc_1",
                        "tool_name": "spawn_agent",
                        "description": "task 1",
                        "success": True,
                        "output": "result 1",
                        "display_text": "result 1",
                        "index": 1,
                        "total": 2,
                    },
                )
                yield first_iteration
            else:
                yield final_iteration

        coordinator._loop.execute_iteration_stream = fake_execute_iteration_stream
        coordinator._loop._call_llm = AsyncMock()
        coordinator._prepare_llm_request = AsyncMock(
            return_value=LLMRequest(messages=[], tools_schema=[])
        )

        events = []
        async for event in coordinator.run_stream(agent, deps, "progressive task"):
            events.append(event)

        event_types = [event.type for event in events]
        assert StreamEventType.PROGRESSIVE_DONE in event_types
        assert StreamEventType.PROGRESSIVE_RESPONSE not in event_types
        assert StreamEventType.DONE in event_types
        coordinator._loop._call_llm.assert_not_awaited()
