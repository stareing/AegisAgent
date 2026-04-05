"""Tests for sub-agent stream queue backpressure in ToolExecutor.

Verifies:
- Queue bounded to _STREAM_QUEUE_MAX_SIZE
- Low-priority (TOKEN) events dropped silently when queue is full
- High-priority events preserved when queue is full (oldest items drained)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.tools.executor import (
    ToolExecutor,
    _BACKPRESSURE_DRAIN_COUNT,
    _LOW_PRIORITY_EVENT_TYPES,
    _STREAM_QUEUE_MAX_SIZE,
)


def _make_executor() -> ToolExecutor:
    """Create a minimal ToolExecutor with a mock registry."""
    registry = MagicMock()
    registry.has_tool.return_value = False
    return ToolExecutor(registry=registry)


def _token_event(spawn_id: str = "sp_1", text: str = "hi") -> StreamEvent:
    """Create a SUBAGENT_STREAM wrapper carrying a TOKEN inner event."""
    return StreamEvent(
        type=StreamEventType.SUBAGENT_STREAM,
        data={"spawn_id": spawn_id, "event_type": "token", "text": text},
    )


def _tool_call_start_event(spawn_id: str = "sp_1") -> StreamEvent:
    """Create a SUBAGENT_STREAM wrapper carrying a tool_call_start inner event."""
    return StreamEvent(
        type=StreamEventType.SUBAGENT_STREAM,
        data={
            "spawn_id": spawn_id,
            "event_type": "tool_call_start",
            "tool_name": "search",
            "tool_call_id": "tc_1",
        },
    )


def _iteration_start_event(spawn_id: str = "sp_1") -> StreamEvent:
    """Create a SUBAGENT_STREAM wrapper carrying an iteration_start inner event."""
    return StreamEvent(
        type=StreamEventType.SUBAGENT_STREAM,
        data={
            "spawn_id": spawn_id,
            "event_type": "iteration_start",
            "iteration_index": 0,
        },
    )


class TestStreamQueueMaxSize:
    """Queue is bounded to _STREAM_QUEUE_MAX_SIZE."""

    def test_queue_has_correct_maxsize(self) -> None:
        executor = _make_executor()
        assert executor._child_stream_queue.maxsize == _STREAM_QUEUE_MAX_SIZE

    def test_accepts_events_up_to_maxsize(self) -> None:
        executor = _make_executor()
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(_token_event(text=f"t{i}"))
        assert executor._child_stream_queue.qsize() == _STREAM_QUEUE_MAX_SIZE


class TestTokenDropOnBackpressure:
    """TOKEN events are dropped silently when queue is full."""

    def test_token_dropped_when_full(self) -> None:
        executor = _make_executor()
        # Fill the queue
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(_token_event(text=f"t{i}"))

        # This TOKEN should be dropped — no exception, queue still full
        executor.enqueue_child_stream_event(_token_event(text="overflow"))
        assert executor._child_stream_queue.qsize() == _STREAM_QUEUE_MAX_SIZE

        # Verify the dropped token is NOT in the queue
        items = []
        while not executor._child_stream_queue.empty():
            items.append(executor._child_stream_queue.get_nowait())
        texts = [e.data.get("text") for e in items]
        assert "overflow" not in texts

    def test_subagent_stream_token_also_dropped(self) -> None:
        """A subagent_stream event whose inner type is 'token' is low-priority."""
        executor = _make_executor()
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(
                _tool_call_start_event()
            )
        # Inner event_type=token → should be dropped
        executor.enqueue_child_stream_event(_token_event(text="drop_me"))
        assert executor._child_stream_queue.qsize() == _STREAM_QUEUE_MAX_SIZE


class TestHighPriorityPreserved:
    """Non-TOKEN events force-drain oldest items and get enqueued."""

    def test_tool_call_start_preserved_when_full(self) -> None:
        executor = _make_executor()
        # Fill with tokens
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(_token_event(text=f"t{i}"))

        high_priority = _tool_call_start_event()
        executor.enqueue_child_stream_event(high_priority)

        # Queue should have drained _BACKPRESSURE_DRAIN_COUNT items and added 1
        expected_size = _STREAM_QUEUE_MAX_SIZE - _BACKPRESSURE_DRAIN_COUNT + 1
        assert executor._child_stream_queue.qsize() == expected_size

        # The high-priority event should be the last item in the queue
        items = []
        while not executor._child_stream_queue.empty():
            items.append(executor._child_stream_queue.get_nowait())
        assert items[-1].data["event_type"] == "tool_call_start"

    def test_iteration_start_preserved_when_full(self) -> None:
        executor = _make_executor()
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(_token_event(text=f"t{i}"))

        high_priority = _iteration_start_event()
        executor.enqueue_child_stream_event(high_priority)

        # Should have been enqueued successfully
        items = []
        while not executor._child_stream_queue.empty():
            items.append(executor._child_stream_queue.get_nowait())
        inner_types = [e.data.get("event_type") for e in items]
        assert "iteration_start" in inner_types

    def test_multiple_high_priority_events_all_preserved(self) -> None:
        executor = _make_executor()
        # Fill queue
        for i in range(_STREAM_QUEUE_MAX_SIZE):
            executor.enqueue_child_stream_event(_token_event(text=f"t{i}"))

        # Enqueue several high-priority events
        for _ in range(5):
            executor.enqueue_child_stream_event(_tool_call_start_event())

        items = []
        while not executor._child_stream_queue.empty():
            items.append(executor._child_stream_queue.get_nowait())

        high_priority_count = sum(
            1 for e in items if e.data.get("event_type") == "tool_call_start"
        )
        assert high_priority_count == 5


class TestExtractEventType:
    """Unit tests for _extract_event_type helper."""

    def test_extracts_inner_event_type_from_subagent_stream(self) -> None:
        event = _token_event()
        assert ToolExecutor._extract_event_type(event) == "token"

    def test_extracts_top_level_type_when_no_inner(self) -> None:
        event = StreamEvent(type=StreamEventType.TOKEN, data={"text": "hi"})
        assert ToolExecutor._extract_event_type(event) == "token"

    def test_returns_empty_for_unknown_object(self) -> None:
        assert ToolExecutor._extract_event_type(object()) == ""


class TestConstants:
    """Verify module-level constants are sane."""

    def test_max_size_is_1000(self) -> None:
        assert _STREAM_QUEUE_MAX_SIZE == 1000

    def test_low_priority_includes_token(self) -> None:
        assert "token" in _LOW_PRIORITY_EVENT_TYPES

    def test_drain_count_positive(self) -> None:
        assert _BACKPRESSURE_DRAIN_COUNT > 0

    def test_drain_count_less_than_max_size(self) -> None:
        assert _BACKPRESSURE_DRAIN_COUNT < _STREAM_QUEUE_MAX_SIZE
