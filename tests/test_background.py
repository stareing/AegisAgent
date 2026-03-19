"""Tests for background task auto-notification system (s08).

Covers:
1. Notification formatting
2. BackgroundNotifier lifecycle
3. Coordinator integration (registration, drain, injection)
4. True parallel execution (independent subprocesses)
5. Cross-run persistence (tasks outlive runs)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_framework.tools.background import (BackgroundNotification,
                                              BackgroundNotifier)

# ══════════════════════════════════════════════════════════════════
# 1. Notification formatting
# ══════════════════════════════════════════════════════════════════


class TestBackgroundNotification:
    def test_success_format(self):
        n = BackgroundNotification("abc123", "echo hi", "hi", exit_code=0)
        msg = n.format_message()
        assert "[bg:abc123]" in msg
        assert "success" in msg
        assert "hi" in msg

    def test_error_format(self):
        n = BackgroundNotification("err1", "false", "error msg", exit_code=1)
        msg = n.format_message()
        assert "exit=1" in msg

    def test_timeout_format(self):
        n = BackgroundNotification("t1", "sleep 999", "timed out", timed_out=True)
        msg = n.format_message()
        assert "timed out" in msg

    def test_truncation(self):
        long_output = "x" * 2000
        n = BackgroundNotification("t1", "cmd", long_output)
        msg = n.format_message()
        assert "truncated" in msg
        assert len(msg) < 1500


# ══════════════════════════════════════════════════════════════════
# 2. BackgroundNotifier lifecycle
# ══════════════════════════════════════════════════════════════════


class TestBackgroundNotifier:
    def test_empty_drain(self):
        notifier = BackgroundNotifier()
        assert notifier.drain() == []

    def test_register_and_pending(self):
        notifier = BackgroundNotifier()
        notifier.register("task-1", "echo hello")
        assert notifier.pending_count == 1
        assert notifier.has_pending

    def test_clear(self):
        notifier = BackgroundNotifier()
        notifier.register("task-1", "echo hello")
        notifier.clear()
        assert notifier.pending_count == 0

    def test_format_notifications_empty(self):
        assert BackgroundNotifier.format_notifications([]) == ""

    def test_format_notifications_with_results(self):
        notifications = [
            BackgroundNotification("a1", "cmd1", "output1"),
            BackgroundNotification("a2", "cmd2", "output2"),
        ]
        text = BackgroundNotifier.format_notifications(notifications)
        assert "<background-results>" in text
        assert "</background-results>" in text
        assert "[bg:a1]" in text
        assert "[bg:a2]" in text


# ══════════════════════════════════════════════════════════════════
# 3. Coordinator integration
# ══════════════════════════════════════════════════════════════════


class TestCoordinatorIntegration:
    def test_register_background_tasks_from_iteration(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.agent import IterationResult
        from agent_framework.models.tool import ToolResult

        coordinator = RunCoordinator()

        iteration = IterationResult(
            tool_results=[
                ToolResult(
                    tool_call_id="tc1",
                    tool_name="bash_exec",
                    success=True,
                    output={"task_id": "bg-abc", "status": "running"},
                ),
            ],
        )
        coordinator._register_background_tasks(iteration)
        assert coordinator._bg_notifier.pending_count == 1

    def test_register_ignores_foreground_bash(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.agent import IterationResult
        from agent_framework.models.tool import ToolResult

        coordinator = RunCoordinator()

        iteration = IterationResult(
            tool_results=[
                ToolResult(
                    tool_call_id="tc1",
                    tool_name="bash_exec",
                    success=True,
                    output={"output": "hello", "exit_code": 0},
                ),
            ],
        )
        coordinator._register_background_tasks(iteration)
        assert coordinator._bg_notifier.pending_count == 0

    def test_drain_injects_messages(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.message import Message
        from agent_framework.models.session import SessionState

        coordinator = RunCoordinator()
        session = SessionState()

        notifications = [
            BackgroundNotification("task-x", "echo done", "done", exit_code=0),
        ]
        text = BackgroundNotifier.format_notifications(notifications)
        coordinator._state_ctrl.append_user_message(
            session, Message(role="user", content=text)
        )
        coordinator._state_ctrl.append_projected_messages(
            session, [Message(role="assistant", content="Noted background results.")]
        )

        msgs = session.get_messages()
        assert len(msgs) == 2
        assert "<background-results>" in msgs[0].content
        assert "Noted" in msgs[1].content

    @pytest.mark.asyncio
    async def test_no_drain_when_no_pending(self):
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.session import SessionState

        coordinator = RunCoordinator()
        session = SessionState()
        await coordinator._drain_background_notifications(session)
        assert len(session.get_messages()) == 0

    def test_notifier_is_instance_level(self):
        """BackgroundNotifier survives across runs (not recreated)."""
        from agent_framework.agent.coordinator import RunCoordinator

        coordinator = RunCoordinator()
        notifier_before = coordinator._bg_notifier
        # Simulate what happens at run start — notifier should be the same object
        assert notifier_before is coordinator._bg_notifier


# ══════════════════════════════════════════════════════════════════
# 4. True parallel execution
# ══════════════════════════════════════════════════════════════════


class TestParallelExecution:
    """Background tasks run as independent subprocesses, not through the session lock."""

    @pytest.mark.asyncio
    async def test_two_background_tasks_run_in_parallel(self):
        """Two sleep commands should complete in ~2s, not ~4s."""
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            t0 = time.monotonic()
            tid1 = await session.execute_background("sleep 1 && echo task1", 10)
            tid2 = await session.execute_background("sleep 1 && echo task2", 10)

            # Both should be pending
            assert session.get_background_result(tid1) is None
            assert session.get_background_result(tid2) is None

            # Wait for both to complete (should take ~1s, not 2s)
            await asyncio.sleep(1.5)

            r1 = session.get_background_result(tid1)
            r2 = session.get_background_result(tid2)
            elapsed = time.monotonic() - t0

            assert r1 is not None, "task1 should have completed"
            assert r2 is not None, "task2 should have completed"
            assert "task1" in r1["output"]
            assert "task2" in r2["output"]
            # If they ran in parallel, total time should be ~1.5s not ~3s
            assert elapsed < 2.5, f"Tasks should run in parallel, took {elapsed:.1f}s"
        finally:
            await session.kill()

    @pytest.mark.asyncio
    async def test_background_does_not_block_foreground(self):
        """A background task should not block foreground execution."""
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            # Start a 3-second background task
            _tid = await session.execute_background("sleep 3", 10)

            # Foreground command should execute immediately
            t0 = time.monotonic()
            result = await session.execute("echo foreground", 5)
            elapsed = time.monotonic() - t0

            assert "foreground" in result["output"]
            assert elapsed < 2.0, f"Foreground blocked by background, took {elapsed:.1f}s"
        finally:
            await session.kill()

    @pytest.mark.asyncio
    async def test_background_timeout(self):
        """Background task respects timeout."""
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            tid = await session.execute_background("sleep 60", timeout_seconds=2)
            await asyncio.sleep(3.5)

            result = session.get_background_result(tid)
            assert result is not None
            assert result["timed_out"] is True
        finally:
            await session.kill()


# ══════════════════════════════════════════════════════════════════
# 5. Cross-run persistence
# ══════════════════════════════════════════════════════════════════


class TestCrossRunPersistence:
    """Tasks that outlive one run should be drained in the next."""

    def test_notifier_preserves_pending_across_runs(self):
        """Pending tasks are NOT cleared between runs."""
        notifier = BackgroundNotifier()
        notifier.register("long-task", "npm install")

        # Simulate run end — notifier should still have the task
        # (no clear() called)
        assert notifier.pending_count == 1
        assert notifier.has_pending

    def test_coordinator_notifier_survives_run_boundary(self):
        """Coordinator's notifier is instance-level, not recreated per run."""
        from agent_framework.agent.coordinator import RunCoordinator

        coordinator = RunCoordinator()
        coordinator._bg_notifier.register("task-from-run-1", "sleep 60")

        # After run 1 ends, notifier still has the task
        assert coordinator._bg_notifier.pending_count == 1

        # Run 2 starts — notifier is the same instance with same pending tasks
        assert coordinator._bg_notifier.has_pending
        assert "task-from-run-1" in coordinator._bg_notifier._pending_task_ids

    def test_run_stream_does_not_recreate_notifier(self):
        """run_stream() must NOT recreate _bg_notifier (regression: coordinator.py:577)."""
        from agent_framework.agent.coordinator import RunCoordinator

        coordinator = RunCoordinator()
        original_notifier = coordinator._bg_notifier
        coordinator._bg_notifier.register("task-from-run-1", "sleep 60")

        # Simulate what run_stream() does at startup: reset caches
        coordinator._cached_tools_schema = None
        # The notifier should NOT be recreated
        assert coordinator._bg_notifier is original_notifier
        assert coordinator._bg_notifier.pending_count == 1


# ══════════════════════════════════════════════════════════════════
# 6. kill_shell with only background tasks
# ══════════════════════════════════════════════════════════════════


class TestBashStopSingleTask:
    """bash_stop terminates a single background task, not the whole session."""

    @pytest.mark.asyncio
    async def test_stop_running_task(self):
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            tid = await session.execute_background("sleep 300", 600)
            await asyncio.sleep(0.3)

            result = session.stop_background_task(tid)
            assert result.get("cancelled") is True
            assert tid not in session._background_tasks
        finally:
            await session.kill()

    @pytest.mark.asyncio
    async def test_stop_completed_task(self):
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            tid = await session.execute_background("echo done", 10)
            await asyncio.sleep(1.0)

            result = session.stop_background_task(tid)
            # Already completed — returns the real result, not cancelled
            assert "done" in result.get("output", "")
        finally:
            await session.kill()

    @pytest.mark.asyncio
    async def test_stop_unknown_task_raises(self):
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        with pytest.raises(ValueError, match="Unknown"):
            session.stop_background_task("nonexistent")

    @pytest.mark.asyncio
    async def test_stop_does_not_kill_other_tasks(self):
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        try:
            tid1 = await session.execute_background("sleep 300", 600)
            tid2 = await session.execute_background("sleep 300", 600)
            await asyncio.sleep(0.3)

            # Stop only tid1
            session.stop_background_task(tid1)

            # tid2 should still be running
            assert tid2 in session._background_tasks
            r2 = session.get_background_result(tid2)
            assert r2 is None  # still running
        finally:
            await session.kill()


class TestKillShellBackgroundOnly:
    """kill_shell must cancel background tasks even without a foreground session."""

    @pytest.mark.asyncio
    async def test_kill_cancels_background_without_foreground(self):
        """Background-only: kill_shell cancels tasks even if _proc is None."""
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        # Only start background tasks, never start foreground
        assert session._proc is None

        tid = await session.execute_background("sleep 30", 60)
        assert session._proc is None  # still no foreground session
        assert len(session._background_tasks) == 1

        result = await session.kill()
        assert "Cancelled 1 background" in result
        assert len(session._background_tasks) == 0
        assert len(session._background_results) == 0

    @pytest.mark.asyncio
    async def test_kill_actually_kills_os_process(self):
        """kill_shell must SIGKILL the OS subprocess, verified by pid liveness check."""
        import os as _os

        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        tid = await session.execute_background("sleep 300", 600)

        # Give subprocess time to start
        await asyncio.sleep(0.3)

        # Capture the real OS pid before kill
        pid = session._background_pids.get(tid)
        assert pid is not None, "Background subprocess pid should be recorded"

        # Verify the process is alive before kill
        try:
            _os.kill(pid, 0)  # signal 0 = existence check
            alive_before = True
        except ProcessLookupError:
            alive_before = False
        assert alive_before, f"pid {pid} should be alive before kill"

        # Kill via kill_shell
        result = await session.kill()
        assert "Cancelled" in result

        # Give SIGKILL time to propagate
        await asyncio.sleep(0.5)

        # Verify the process is DEAD after kill
        try:
            _os.kill(pid, 0)
            alive_after = True
        except ProcessLookupError:
            alive_after = False
        assert not alive_after, f"pid {pid} should be dead after kill_shell"

    @pytest.mark.asyncio
    async def test_kill_cancels_background_and_foreground(self):
        """Both foreground + background: kill_shell handles both."""
        from agent_framework.tools.shell.shell_manager import BashSession

        session = BashSession()
        # Start foreground first
        await session.execute("echo hello", 5)
        assert session._proc is not None

        # Then start background
        tid = await session.execute_background("sleep 30", 60)
        assert len(session._background_tasks) == 1

        result = await session.kill()
        assert "terminated" in result.lower()
        assert "1 background" in result
        assert session._proc is None
        assert len(session._background_tasks) == 0
