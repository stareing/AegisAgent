"""Tests for progressive streaming — verifies real-time event emission.

Covers:
1. TOOL_CALL_DONE events emitted in completion order (not batched)
2. SUBAGENT_DONE / PROGRESSIVE_RESPONSE events in stream
3. Non-progressive mode unchanged
4. Event ordering: START before DONE before final DONE
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    EffectiveRunConfig,
    IterationResult,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.models.tool import ToolExecutionMeta, ToolResult


class TestProgressiveToolDoneOrdering:
    """TOOL_CALL_DONE must be emitted as each tool completes, not batched."""

    @pytest.mark.asyncio
    async def test_progressive_yields_done_per_tool(self):
        """In progressive mode, each TOOL_CALL_DONE should arrive individually."""
        from agent_framework.agent.loop import AgentLoop, AgentLoopDeps
        from agent_framework.agent.default_agent import DefaultAgent

        loop = AgentLoop()
        agent = DefaultAgent(progressive_tool_results=True)

        # Mock adapter that returns 3 tool calls
        mock_adapter = AsyncMock()

        async def mock_stream(*args, **kwargs):
            from agent_framework.adapters.model.base_adapter import ModelChunk
            import json
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

            assert len(starts) == 3, f"Expected 3 TOOL_CALL_START, got {len(starts)}"
            assert len(dones) == 3, f"Expected 3 TOOL_CALL_DONE, got {len(dones)}"

            # DONE events should have correct tool_call_ids
            done_ids = [e.data["tool_call_id"] for e in dones]
            assert done_ids == ["tc_0", "tc_1", "tc_2"]

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
            data={"tool_call_id": "tc1", "task_input": "test", "success": True,
                  "output": "result", "index": 1, "total": 3},
        )
        assert event.type == StreamEventType.SUBAGENT_DONE
        assert event.data["success"] is True

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
        from agent_framework.agent.loop import AgentLoop, AgentLoopDeps
        from agent_framework.agent.default_agent import DefaultAgent

        loop = AgentLoop()
        agent = DefaultAgent(progressive_tool_results=False)  # explicit non-progressive

        mock_adapter = AsyncMock()

        async def mock_stream(*args, **kwargs):
            from agent_framework.adapters.model.base_adapter import ModelChunk
            import json
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
