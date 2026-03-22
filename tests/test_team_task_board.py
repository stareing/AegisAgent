"""Tests for TeamTaskBoard — shared task panel with claim, deps, concurrency.

Verifies:
1. Create task returns valid task_id + status.
2. Claim atomically assigns to requester.
3. Auto-claim picks first PENDING unblocked task.
4. Concurrent claims: only one succeeds.
5. Dependencies: task B blocked until A completes.
6. Complete auto-unblocks dependents.
7. List/filter by status and assignee.
8. Fail marks task as failed.
9. Team coordinator task board methods work.
10. Tool actions create_task/claim/complete_task/list_tasks route correctly.
"""

from __future__ import annotations

import threading

import pytest

from agent_framework.team.task_board import (
    TaskStatus,
    TeamTask,
    TeamTaskBoard,
)


@pytest.fixture
def board() -> TeamTaskBoard:
    return TeamTaskBoard("test_team")


class TestCreateTask:
    def test_creates_with_pending_status(self, board: TeamTaskBoard):
        task = board.create_task("Fix bug")
        assert task.title == "Fix bug"
        assert task.status == TaskStatus.PENDING
        assert task.task_id.startswith("task_")

    def test_creates_with_description(self, board: TeamTaskBoard):
        task = board.create_task("Fix bug", description="Null pointer in parser")
        assert task.description == "Null pointer in parser"

    def test_blocked_when_deps_unresolved(self, board: TeamTaskBoard):
        a = board.create_task("Task A")
        b = board.create_task("Task B", depends_on=[a.task_id])
        assert b.status == TaskStatus.BLOCKED

    def test_pending_when_deps_resolved(self, board: TeamTaskBoard):
        a = board.create_task("Task A")
        board.complete_task(a.task_id)
        c = board.create_task("Task C", depends_on=[a.task_id])
        assert c.status == TaskStatus.PENDING

    def test_pending_when_no_deps(self, board: TeamTaskBoard):
        task = board.create_task("Solo task")
        assert task.status == TaskStatus.PENDING


class TestClaimTask:
    def test_claim_specific_task(self, board: TeamTaskBoard):
        task = board.create_task("Fix bug")
        claimed = board.claim_task("coder_1", task.task_id)
        assert claimed is not None
        assert claimed.status == TaskStatus.IN_PROGRESS
        assert claimed.assigned_to == "coder_1"

    def test_claim_blocked_task_returns_none(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B", depends_on=[a.task_id])
        result = board.claim_task("coder_1", b.task_id)
        assert result is None

    def test_auto_claim_picks_first_pending(self, board: TeamTaskBoard):
        t1 = board.create_task("First")
        t2 = board.create_task("Second")
        claimed = board.claim_task("coder_1")
        assert claimed is not None
        assert claimed.task_id == t1.task_id

    def test_auto_claim_skips_blocked(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B", depends_on=[a.task_id])
        c = board.create_task("C")
        claimed = board.claim_task("coder_1")
        assert claimed.task_id == a.task_id
        claimed2 = board.claim_task("coder_2")
        assert claimed2.task_id == c.task_id  # Skips B (blocked)

    def test_auto_claim_returns_none_when_empty(self, board: TeamTaskBoard):
        assert board.claim_task("coder_1") is None

    def test_double_claim_fails(self, board: TeamTaskBoard):
        task = board.create_task("Fix bug")
        board.claim_task("coder_1", task.task_id)
        result = board.claim_task("coder_2", task.task_id)
        assert result is None  # Already IN_PROGRESS


class TestConcurrentClaim:
    def test_only_one_thread_wins(self, board: TeamTaskBoard):
        board.create_task("Shared task")
        results = []

        def try_claim(agent_id: str):
            r = board.claim_task(agent_id)
            results.append((agent_id, r is not None))

        threads = [
            threading.Thread(target=try_claim, args=(f"agent_{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [agent_id for agent_id, won in results if won]
        assert len(winners) == 1


class TestCompleteTask:
    def test_marks_completed(self, board: TeamTaskBoard):
        task = board.create_task("A")
        board.claim_task("coder", task.task_id)
        result = board.complete_task(task.task_id, result="Done")
        assert result is not None
        assert result.status == TaskStatus.COMPLETED
        assert result.result == "Done"

    def test_auto_unblocks_dependent(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B", depends_on=[a.task_id])
        assert b.status == TaskStatus.BLOCKED

        board.claim_task("coder", a.task_id)
        board.complete_task(a.task_id)

        updated_b = board.get_task(b.task_id)
        assert updated_b.status == TaskStatus.PENDING

    def test_multi_dep_unblock(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B")
        c = board.create_task("C", depends_on=[a.task_id, b.task_id])
        assert c.status == TaskStatus.BLOCKED

        board.complete_task(a.task_id)
        assert board.get_task(c.task_id).status == TaskStatus.BLOCKED  # B still pending

        board.complete_task(b.task_id)
        assert board.get_task(c.task_id).status == TaskStatus.PENDING  # Both done

    def test_complete_already_completed_returns_none(self, board: TeamTaskBoard):
        task = board.create_task("A")
        board.complete_task(task.task_id)
        assert board.complete_task(task.task_id) is None


class TestFailTask:
    def test_marks_failed(self, board: TeamTaskBoard):
        task = board.create_task("A")
        result = board.fail_task(task.task_id, "timeout")
        assert result.status == TaskStatus.FAILED
        assert result.result == "timeout"


class TestListAndFilter:
    def test_list_all(self, board: TeamTaskBoard):
        board.create_task("A")
        board.create_task("B")
        assert len(board.list_tasks()) == 2

    def test_filter_by_status(self, board: TeamTaskBoard):
        a = board.create_task("A")
        board.create_task("B")
        board.claim_task("coder", a.task_id)
        in_progress = board.list_tasks(status=TaskStatus.IN_PROGRESS)
        assert len(in_progress) == 1
        assert in_progress[0].task_id == a.task_id

    def test_filter_by_assignee(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B")
        board.claim_task("coder", a.task_id)
        board.claim_task("reviewer", b.task_id)
        coder_tasks = board.list_tasks(assigned_to="coder")
        assert len(coder_tasks) == 1

    def test_list_claimable(self, board: TeamTaskBoard):
        a = board.create_task("A")
        b = board.create_task("B", depends_on=[a.task_id])
        c = board.create_task("C")
        claimable = board.list_claimable()
        ids = {t.task_id for t in claimable}
        assert a.task_id in ids
        assert c.task_id in ids
        assert b.task_id not in ids

    def test_task_count(self, board: TeamTaskBoard):
        board.create_task("A")
        a = board.create_task("B")
        board.create_task("C", depends_on=[a.task_id])
        counts = board.task_count()
        assert counts["pending"] == 2
        assert counts["blocked"] == 1


class TestCoordinatorTaskBoard:
    """Test task board methods on TeamCoordinator."""

    def _make_coordinator(self):
        from datetime import datetime, timezone
        from agent_framework.models.team import TeamMember, TeamMemberStatus
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.coordinator import TeamCoordinator
        from agent_framework.team.mailbox import TeamMailbox
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.registry import TeamRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry

        bus = AgentBus(persistence=InMemoryBusPersistence())
        registry = TeamRegistry("test_team")
        mailbox = TeamMailbox(bus, registry)
        lead = TeamMember(agent_id="lead", team_id="test_team", role="lead",
                          status=TeamMemberStatus.WORKING)
        registry.register(lead)
        coord = TeamCoordinator("test_team", "lead", mailbox, registry,
                                PlanRegistry(), ShutdownRegistry())
        return coord

    def test_create_task_initializes_board(self):
        coord = self._make_coordinator()
        result = coord.create_task("Fix parser")
        assert result["created"] is True
        assert "task_id" in result
        assert coord._task_board is not None

    def test_claim_without_board_returns_error(self):
        coord = self._make_coordinator()
        result = coord.claim_task("coder")
        assert result["claimed"] is False

    def test_full_lifecycle(self):
        coord = self._make_coordinator()
        created = coord.create_task("A")
        claimed = coord.claim_task("coder", created["task_id"])
        assert claimed["claimed"] is True
        completed = coord.complete_task(created["task_id"], result="Done")
        assert completed["completed"] is True

    def test_list_tasks(self):
        coord = self._make_coordinator()
        coord.create_task("A")
        coord.create_task("B")
        result = coord.list_tasks()
        assert result["total"] == 2

    def test_status_includes_task_board(self):
        coord = self._make_coordinator()
        coord.create_task("A")
        status = coord.get_team_status(caller_id="lead")
        assert "task_board" in status
        assert status["task_board"]["pending"] == 1
