"""Tests for spawn_id tracking — watcher must use runtime's actual_sid.

Regression guard: spawn_async() may return a different ID than the spec's
spawn_id. The watcher must poll using the runtime's actual ID, not the
locally generated one.

Verifies:
1. _assign_task_async passes actual_sid to _watch_teammate (not spec spawn_id).
2. _spawn_continuation returns actual_sid from runtime (not local new_spawn_id).
3. When actual_sid differs from spec, result collection still works.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from datetime import datetime, timezone

from agent_framework.models.team import (
    TeamMember,
    TeamMemberStatus,
)
from agent_framework.notification.bus import AgentBus
from agent_framework.notification.persistence import InMemoryBusPersistence
from agent_framework.team.coordinator import TeamCoordinator
from agent_framework.team.mailbox import TeamMailbox
from agent_framework.team.plan_registry import PlanRegistry
from agent_framework.team.registry import TeamRegistry
from agent_framework.team.shutdown_registry import ShutdownRegistry


@pytest.fixture
def coordinator_with_mock_runtime():
    team_id = "test_team"
    bus = AgentBus(persistence=InMemoryBusPersistence())
    registry = TeamRegistry(team_id)
    mailbox = TeamMailbox(bus, registry)

    lead = TeamMember(
        agent_id="lead_001", team_id=team_id, role="lead",
        status=TeamMemberStatus.WORKING,
    )
    registry.register(lead)

    coder = TeamMember(
        agent_id="role_coder", team_id=team_id, role="coder",
        status=TeamMemberStatus.IDLE,
    )
    registry.register(coder)

    coordinator = TeamCoordinator(
        team_id=team_id,
        lead_agent_id="lead_001",
        mailbox=mailbox,
        team_registry=registry,
        plan_registry=PlanRegistry(),
        shutdown_registry=ShutdownRegistry(),
    )

    # Mock runtime that returns a DIFFERENT actual_sid than the spec
    mock_runtime = MagicMock()
    mock_runtime.spawn_async = AsyncMock(return_value="runtime_actual_id_xyz")
    mock_runtime.collect_result = AsyncMock(return_value=None)
    coordinator._runtime = mock_runtime

    return coordinator, mock_runtime, registry


class TestAssignUsesActualSid:
    @pytest.mark.asyncio
    async def test_watcher_receives_actual_sid(self, coordinator_with_mock_runtime):
        """_assign_task_async must pass actual_sid to _watch_teammate, not spec spawn_id."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime

        watched_spawn_ids = []

        # Patch _watch_teammate to capture which spawn_id it receives
        original_watch = coordinator._watch_teammate

        async def capture_watch(agent_id, spawn_id, role, task):
            watched_spawn_ids.append(spawn_id)
            # Don't actually poll — just record

        coordinator._watch_teammate = capture_watch

        await coordinator._assign_task_async("test task", "role_coder")

        # Give asyncio.create_task a chance to run
        await asyncio.sleep(0.1)

        # The watcher must have received the runtime's actual_sid
        assert len(watched_spawn_ids) == 1
        assert watched_spawn_ids[0] == "runtime_actual_id_xyz"
        # NOT the locally generated spawn_id (which would be a 12-char hex)
        assert watched_spawn_ids[0] != coordinator._runtime.spawn_async.call_args


class TestContinuationUsesActualSid:
    @pytest.mark.asyncio
    async def test_spawn_continuation_returns_actual_sid(self, coordinator_with_mock_runtime):
        """_spawn_continuation must return runtime's actual_sid, not local new_spawn_id."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime

        # Mock returns a specific ID
        mock_runtime.spawn_async = AsyncMock(return_value="continuation_actual_abc")

        result = await coordinator._spawn_continuation(
            agent_id="role_coder",
            role="coder",
            task="original task",
            conversation_history=["[Original Task] test"],
            answer="the file is at ./test.py",
        )

        assert result == "continuation_actual_abc"
        # Verify spawn_async was called
        mock_runtime.spawn_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_continuation_failure_returns_none(self, coordinator_with_mock_runtime):
        """_spawn_continuation returns None when runtime raises."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime

        mock_runtime.spawn_async = AsyncMock(side_effect=RuntimeError("spawn failed"))

        result = await coordinator._spawn_continuation(
            agent_id="role_coder",
            role="coder",
            task="task",
            conversation_history=[],
            answer="answer",
        )

        assert result is None


class TestFinalizeIsAwaited:
    @pytest.mark.asyncio
    async def test_finalize_is_coroutine(self, coordinator_with_mock_runtime):
        """_finalize_teammate_result is async and must be awaited."""
        coordinator, _, _ = coordinator_with_mock_runtime
        import inspect
        assert inspect.iscoroutinefunction(coordinator._finalize_teammate_result)

    @pytest.mark.asyncio
    async def test_finalize_sets_result_ready(self, coordinator_with_mock_runtime):
        """When awaited, _finalize_teammate_result sets RESULT_READY status."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime

        # Create a fake result
        from unittest.mock import MagicMock
        fake_result = MagicMock()
        fake_result.success = True
        fake_result.final_answer = "Task done"
        fake_result.error = None
        fake_result.iterations_used = 3

        # Set member to WORKING first
        registry.update_status("role_coder", TeamMemberStatus.WORKING)

        await coordinator._finalize_teammate_result(
            agent_id="role_coder", spawn_id="test_spawn",
            role="coder", task="test task", result=fake_result,
        )

        m = registry.get("role_coder")
        assert m.status == TeamMemberStatus.RESULT_READY


class TestMailIdentityConsistency:
    @pytest.mark.asyncio
    async def test_spawn_spec_uses_member_agent_id(self, coordinator_with_mock_runtime):
        """SubAgentSpec.spawn_id should be the member's agent_id, not random hex."""
        coordinator, mock_runtime, _ = coordinator_with_mock_runtime

        await coordinator._assign_task_async("test task", "role_coder")

        # Verify the spec passed to spawn_async
        call_args = mock_runtime.spawn_async.call_args
        spec = call_args[0][0]  # First positional arg
        assert spec.spawn_id == "role_coder"


class TestBusyAssignmentGuard:
    def test_assign_task_rejects_working_member(self, coordinator_with_mock_runtime):
        """assign_task must reject reassigning a busy teammate."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime
        registry.update_status("role_coder", TeamMemberStatus.WORKING)

        result = coordinator.assign_task("second task", "role_coder")

        assert result["assigned"] is False
        assert "busy" in result["error"]
        mock_runtime.spawn_async.assert_not_called()

    def test_assign_task_rejects_waiting_member(self, coordinator_with_mock_runtime):
        """assign_task must reject when teammate is waiting for answer."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime
        registry.update_status("role_coder", TeamMemberStatus.WAITING_ANSWER)

        result = coordinator.assign_task("second task", "role_coder")

        assert result["assigned"] is False
        assert "busy" in result["error"]

    def test_concurrent_assign_blocked_by_atomic_claim(self, coordinator_with_mock_runtime):
        """Two rapid assign_task calls: first succeeds, second is rejected."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime

        r1 = coordinator.assign_task("task one", "role_coder")
        r2 = coordinator.assign_task("task two", "role_coder")

        assert r1["assigned"] is True
        assert r2["assigned"] is False
        assert "busy" in r2["error"]


class TestAssignSpawnFailureHandling:
    @pytest.mark.asyncio
    async def test_assign_task_async_marks_failed_on_spawn_error(self, coordinator_with_mock_runtime):
        """A spawn failure must not leave the member stuck in WORKING."""
        coordinator, mock_runtime, registry = coordinator_with_mock_runtime
        mock_runtime.spawn_async = AsyncMock(side_effect=RuntimeError("boom"))
        registry.update_status("role_coder", TeamMemberStatus.WORKING)

        await coordinator._assign_task_async("task", "role_coder")

        member = registry.get("role_coder")
        assert member is not None
        assert member.status == TeamMemberStatus.FAILED

        events = coordinator.process_inbox()
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "Failed to start teammate task" in events[0]["error"]


class TestPollForResultUsesCorrectId:
    @pytest.mark.asyncio
    async def test_poll_uses_given_spawn_id(self, coordinator_with_mock_runtime):
        """_poll_for_result must call collect_result with the spawn_id it receives."""
        coordinator, mock_runtime, _ = coordinator_with_mock_runtime
        from agent_framework.models.subagent import SubAgentResult

        call_ids = []

        async def track_collect(spawn_id, wait=False):
            call_ids.append(spawn_id)
            # Return result on 3rd call so poll terminates
            if len(call_ids) >= 3:
                return SubAgentResult(spawn_id=spawn_id, success=True, final_answer="done")
            return None

        mock_runtime.collect_result = track_collect

        result = await coordinator._poll_for_result("the_correct_id")

        assert len(call_ids) == 3
        assert all(sid == "the_correct_id" for sid in call_ids)
        assert result is not None
