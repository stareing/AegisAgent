"""Tests for RunDispatcher — serialized conversation turns.

Verifies:
1. User turns are serialized (no concurrent runs).
2. Team notification turns do not overlap with user turns.
3. Notification batching window works.
4. Dispatcher can be started and stopped cleanly.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_framework.agent.run_dispatcher import RunDispatcher


@pytest.fixture
def call_log():
    return {"user_calls": [], "notification_calls": [], "running": 0}


@pytest.fixture
def dispatcher(call_log):
    async def run_user(text, **kwargs):
        call_log["running"] += 1
        assert call_log["running"] == 1, "Concurrent run detected!"
        call_log["user_calls"].append(text)
        await asyncio.sleep(0.05)
        call_log["running"] -= 1
        return f"response_{text}"

    async def run_notification():
        call_log["running"] += 1
        assert call_log["running"] == 1, "Concurrent run detected!"
        call_log["notification_calls"].append("notified")
        await asyncio.sleep(0.02)
        call_log["running"] -= 1

    d = RunDispatcher(
        run_user_turn=run_user,
        run_notification_turn=run_notification,
        batch_window_ms=50,
    )
    return d


class TestDispatcherBasics:
    @pytest.mark.asyncio
    async def test_user_turn_returns_result(self, dispatcher, call_log):
        result = await dispatcher.submit_user_turn("hello")
        assert result == "response_hello"
        assert call_log["user_calls"] == ["hello"]

    @pytest.mark.asyncio
    async def test_sequential_user_turns(self, dispatcher, call_log):
        await dispatcher.submit_user_turn("a")
        await dispatcher.submit_user_turn("b")
        assert call_log["user_calls"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_notification_turn_triggered(self, dispatcher, call_log):
        dispatcher.start()
        try:
            dispatcher.submit_team_notification()
            # Wait for batch window + execution
            await asyncio.sleep(0.2)
            assert len(call_log["notification_calls"]) >= 1
        finally:
            dispatcher.stop()

    @pytest.mark.asyncio
    async def test_no_concurrent_runs(self, dispatcher, call_log):
        """Submit user turn and notification simultaneously — must not overlap."""
        dispatcher.start()
        try:
            # Submit notification, then immediately submit user turn
            dispatcher.submit_team_notification()
            result = await dispatcher.submit_user_turn("user_msg")
            assert result == "response_user_msg"
            # Wait for notification to also complete
            await asyncio.sleep(0.2)
            # Both should have run, but never concurrently (assertion in callbacks)
            assert len(call_log["user_calls"]) == 1
        finally:
            dispatcher.stop()


class TestDispatcherLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, dispatcher):
        dispatcher.start()
        assert dispatcher._poll_task is not None
        assert not dispatcher._poll_task.done()
        dispatcher.stop()
        await asyncio.sleep(0.1)
        assert dispatcher._shutdown is True

    @pytest.mark.asyncio
    async def test_stop_without_start(self, dispatcher):
        dispatcher.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_double_start(self, dispatcher):
        dispatcher.start()
        task1 = dispatcher._poll_task
        dispatcher.start()  # Should reuse or replace cleanly
        dispatcher.stop()
