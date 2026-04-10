"""Tests for ProgressSummarizer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_framework.agent.progress_summarizer import ProgressSummarizer
from agent_framework.agent.run_state import RunStateController
from agent_framework.models.agent import AgentState, IterationResult
from agent_framework.models.message import ModelResponse


def _make_agent_state(iteration_count: int = 0) -> AgentState:
    """Create an AgentState with the given number of iterations."""
    state = AgentState(run_id="test-run", task="test task")
    for i in range(iteration_count):
        state.iteration_history.append(
            IterationResult(
                iteration_index=i,
                llm_input_preview=f"iteration {i} preview",
            )
        )
    return state


def _make_mock_adapter(summary_text: str = "Analyzing code files") -> AsyncMock:
    """Create a mock model adapter that returns a fixed summary."""
    adapter = AsyncMock()
    adapter.complete.return_value = ModelResponse(
        content=summary_text,
        finish_reason="stop",
    )
    return adapter


class TestProgressSummarizerStartStop:
    """Summarizer starts and stops cleanly."""

    @pytest.mark.asyncio
    async def test_start_and_stop_cleanly(self):
        adapter = _make_mock_adapter()
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=0.05)
        state = _make_agent_state(iteration_count=2)

        await summarizer.start(state)
        # Let one cycle run
        await asyncio.sleep(0.12)
        result = await summarizer.stop()

        assert result is not None
        assert result == "Analyzing code files"
        assert state.progress_summary == "Analyzing code files"

    @pytest.mark.asyncio
    async def test_stop_without_start_returns_none(self):
        adapter = _make_mock_adapter()
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=1.0)

        result = await summarizer.stop()
        assert result is None

    @pytest.mark.asyncio
    async def test_stop_returns_last_summary(self):
        adapter = _make_mock_adapter("Reading configuration")
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=0.05)
        state = _make_agent_state(iteration_count=3)

        await summarizer.start(state)
        await asyncio.sleep(0.12)
        result = await summarizer.stop()

        assert result == "Reading configuration"


class TestProgressSummarizerDedup:
    """Dedup skips identical summaries."""

    @pytest.mark.asyncio
    async def test_dedup_skips_identical_summaries(self):
        adapter = _make_mock_adapter("Analyzing code files")
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=0.05)
        state = _make_agent_state(iteration_count=2)

        await summarizer.start(state)
        # Wait for two cycles
        await asyncio.sleep(0.15)

        # Add more iterations so the second cycle sees new data
        state.iteration_history.append(
            IterationResult(iteration_index=2, llm_input_preview="new")
        )
        await asyncio.sleep(0.12)
        await summarizer.stop()

        # The adapter was called twice (once per new iteration_count),
        # but the summary is set only when it changes.
        # Both calls return the same text, so dedup should skip the second update.
        assert adapter.complete.call_count >= 2
        assert state.progress_summary == "Analyzing code files"

    @pytest.mark.asyncio
    async def test_no_call_when_no_iterations(self):
        adapter = _make_mock_adapter()
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=0.05)
        state = _make_agent_state(iteration_count=0)

        await summarizer.start(state)
        await asyncio.sleep(0.12)
        await summarizer.stop()

        adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_iteration_count_unchanged(self):
        adapter = _make_mock_adapter("Working on task")
        summarizer = ProgressSummarizer(adapter, RunStateController(), interval_seconds=0.05)
        state = _make_agent_state(iteration_count=1)

        await summarizer.start(state)
        await asyncio.sleep(0.12)
        # First call happens
        first_call_count = adapter.complete.call_count
        assert first_call_count == 1

        # Wait another cycle without changing iterations
        await asyncio.sleep(0.08)
        await summarizer.stop()

        # Should not have made another call since iteration count didn't change
        assert adapter.complete.call_count == 1
