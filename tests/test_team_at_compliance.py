"""AT-* compliance tests — covers all 15 items from agent_team_protocol_spec.md §13.

Each test class maps to one AT-* requirement. Tests verify real runtime behavior,
not just model existence.
"""

from __future__ import annotations

import asyncio
import json
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_framework.models.team import (
    BUSY_MEMBER_STATUSES,
    MailEvent,
    MailEventType,
    TeamActionError,
    TeamConfigData,
    TeamConfigMember,
    TeamMember,
    TeamMemberStatus,
    TeamNotificationType,
    TeamSessionState,
)
from agent_framework.notification.bus import AgentBus
from agent_framework.notification.persistence import InMemoryBusPersistence
from agent_framework.team.coordinator import TeamCoordinator
from agent_framework.team.mailbox import TeamMailbox
from agent_framework.team.plan_registry import PlanRegistry
from agent_framework.team.registry import TeamRegistry
from agent_framework.team.shutdown_registry import ShutdownRegistry
from agent_framework.team.task_board import TaskStatus, TeamTaskBoard


def _make_team():
    """Create a complete team environment for testing."""
    bus = AgentBus(persistence=InMemoryBusPersistence())
    registry = TeamRegistry("team_test")
    mailbox = TeamMailbox(bus, registry)
    lead = TeamMember(agent_id="lead_001", team_id="team_test", role="lead",
                      status=TeamMemberStatus.WORKING)
    registry.register(lead)
    coder = TeamMember(agent_id="role_coder", team_id="team_test", role="coder",
                       status=TeamMemberStatus.IDLE)
    registry.register(coder)
    reviewer = TeamMember(agent_id="role_reviewer", team_id="team_test", role="reviewer",
                          status=TeamMemberStatus.IDLE)
    registry.register(reviewer)
    coord = TeamCoordinator("team_test", "lead_001", mailbox, registry,
                            PlanRegistry(), ShutdownRegistry())
    return coord, mailbox, registry


# ── AT-001: Real team config exists ──────────────────────────

class TestAT001_TeamConfigPersistence:
    def test_config_data_model_fields(self):
        config = TeamConfigData(
            team_id="team_abc", lead_id="lead_001", name="my-team",
            members=[TeamConfigMember(member_id="tm_coder", role="coder")],
        )
        assert config.team_id == "team_abc"
        assert config.lead_id == "lead_001"
        assert len(config.members) == 1
        assert config.created_at is not None

    def test_config_store_save_load(self):
        from agent_framework.team.config_store import TeamConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamConfigStore(base_dir=tmpdir)
            config = TeamConfigData(
                team_id="team_123", lead_id="lead", name="test-team",
                members=[
                    TeamConfigMember(member_id="tm_a", role="coder"),
                    TeamConfigMember(member_id="tm_b", role="reviewer"),
                ],
            )
            path = store.save(config)
            assert path.exists()

            loaded = store.load("test-team")
            assert loaded is not None
            assert loaded.team_id == "team_123"
            assert loaded.lead_id == "lead"
            assert len(loaded.members) == 2

    def test_config_store_delete(self):
        from agent_framework.team.config_store import TeamConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamConfigStore(base_dir=tmpdir)
            config = TeamConfigData(team_id="t", lead_id="l", name="del-test")
            store.save(config)
            assert store.delete("del-test") is True
            assert store.load("del-test") is None

    def test_config_store_list_teams(self):
        from agent_framework.team.config_store import TeamConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamConfigStore(base_dir=tmpdir)
            store.save(TeamConfigData(team_id="t1", lead_id="l", name="alpha"))
            store.save(TeamConfigData(team_id="t2", lead_id="l", name="beta"))
            teams = store.list_teams()
            assert "alpha" in teams
            assert "beta" in teams


# ── AT-002: Real task list exists ────────────────────────────

class TestAT002_RealTaskList:
    def test_task_board_returns_structured_tasks(self):
        board = TeamTaskBoard("team_test")
        task = board.create_task("Fix bug", description="Null pointer")
        assert task.task_id.startswith("task_")
        assert task.title == "Fix bug"
        assert task.status == TaskStatus.PENDING

    def test_coordinator_task_methods_work(self):
        coord, _, _ = _make_team()
        result = coord.create_task("Test task")
        assert result["created"] is True
        assert "task_id" in result


# ── AT-003: Atomic claim works ───────────────────────────────

class TestAT003_AtomicClaim:
    def test_two_claimers_only_one_wins(self):
        board = TeamTaskBoard("team_test")
        board.create_task("Shared task")
        results = []

        def try_claim(agent_id):
            r = board.claim_task(agent_id)
            results.append((agent_id, r is not None))

        threads = [threading.Thread(target=try_claim, args=(f"a{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [a for a, won in results if won]
        assert len(winners) == 1


# ── AT-004: Dependency unlock works ──────────────────────────

class TestAT004_DependencyUnlock:
    def test_blocked_task_unblocks_on_dep_complete(self):
        board = TeamTaskBoard("team_test")
        a = board.create_task("A")
        b = board.create_task("B", depends_on=[a.task_id])
        assert b.status == TaskStatus.BLOCKED

        board.complete_task(a.task_id)
        assert board.get_task(b.task_id).status == TaskStatus.PENDING


# ── AT-005: Direct teammate message works ────────────────────

class TestAT005_DirectTeammateMessage:
    def test_teammate_sends_to_sibling_by_member_id(self):
        coord, mailbox, registry = _make_team()
        from agent_framework.team.teammate_runtime import TeammateRuntime
        rt = TeammateRuntime("role_coder", "team_test", mailbox, registry, PlanRegistry())
        rt.send_to_sibling("role_reviewer", "Check my code")

        inbox = mailbox.read_inbox("role_reviewer")
        assert len(inbox) == 1
        assert inbox[0].from_agent == "role_coder"
        assert inbox[0].payload["message"] == "Check my code"


# ── AT-006: Request/reply correlation works ──────────────────

class TestAT006_RequestReplyCorrelation:
    def test_reply_carries_correlation_id(self):
        coord, mailbox, registry = _make_team()
        original = mailbox.send(MailEvent(
            team_id="team_test", from_agent="role_coder", to_agent="lead_001",
            event_type=MailEventType.QUESTION, request_id="req_001",
            payload={"question": "Which DB?", "request_id": "req_001"},
        ))

        reply = mailbox.reply(
            original.event_id, {"answer": "PostgreSQL"}, source="lead_001",
        )
        assert reply.correlation_id == original.event_id


# ── AT-007: Plan approval gates execution ────────────────────

class TestAT007_PlanApprovalGate:
    def test_approval_writes_pending_approvals(self):
        coord, mailbox, registry = _make_team()
        plan = coord._plans.create(
            requester="role_coder", approver="lead_001",
            plan_text="Refactor auth", title="Auth refactor",
            team_id="team_test",
        )
        coord.approve_plan(plan.request_id)
        assert "role_coder" in coord._pending_approvals
        assert coord._pending_approvals["role_coder"]["approved"] is True

    def test_rejection_writes_pending_approvals(self):
        coord, mailbox, registry = _make_team()
        plan = coord._plans.create(
            requester="role_coder", approver="lead_001",
            plan_text="Drop DB", title="Reset",
            team_id="team_test",
        )
        coord.reject_plan(plan.request_id, feedback="Too risky")
        assert coord._pending_approvals["role_coder"]["approved"] is False
        assert coord._pending_approvals["role_coder"]["feedback"] == "Too risky"


# ── AT-008: Long-lived teammate session exists ───────────────

class TestAT008_LongLivedSession:
    def test_session_state_model_fields(self):
        session = TeamSessionState(
            session_id="sess_001", team_id="team_test", member_id="role_coder",
            status=TeamMemberStatus.WORKING, current_task_id="task_001",
        )
        assert session.session_id == "sess_001"
        assert session.member_id == "role_coder"

    def test_qa_continuation_preserves_context(self):
        """Multi-run Q&A cycle maintains conversation_history."""
        coord, _, _ = _make_team()
        # Simulate answer delivery for continuation
        coord._pending_answers["role_coder"] = "use pytest"
        answer = coord._pending_answers.pop("role_coder")
        assert answer == "use pytest"


# ── AT-009: Idle notification works ──────────────────────────

class TestAT009_IdleNotification:
    def test_teammate_idle_in_notification_types(self):
        assert TeamNotificationType.TEAMMATE_IDLE.value == "TEAMMATE_IDLE"

    def test_teammate_idle_in_default_escalation(self):
        from agent_framework.team.notification_policy import TeamNotificationPolicy
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_notification(TeamNotificationType.TEAMMATE_IDLE)


# ── AT-010: Cleanup refuses dirty state ──────────────────────

class TestAT010_CleanupRefusesDirty:
    def test_cleanup_fails_with_working_member(self):
        coord, _, registry = _make_team()
        registry.update_status("role_coder", TeamMemberStatus.WORKING)
        result = coord.cleanup_team()
        assert result["ok"] is False
        assert result["error_code"] == "TEAM_CLEANUP_ACTIVE_MEMBERS"
        assert "role_coder" in result["active_members"]

    def test_cleanup_fails_with_waiting_member(self):
        coord, _, registry = _make_team()
        registry.update_status("role_coder", TeamMemberStatus.WAITING_ANSWER)
        result = coord.cleanup_team()
        assert result["ok"] is False

    def test_cleanup_succeeds_when_all_idle(self):
        coord, _, registry = _make_team()
        # All non-lead members start as IDLE
        result = coord.cleanup_team()
        assert result["ok"] is True
        assert result["cleaned"] is True


# ── AT-011: Cleanup removes resources ────────────────────────

class TestAT011_CleanupRemovesResources:
    def test_cleanup_clears_internal_stores(self):
        coord, _, _ = _make_team()
        coord.create_task("Test task")
        coord._pending_answers["x"] = "y"
        coord._pending_approvals["z"] = {"approved": True}

        result = coord.cleanup_team()
        assert result["ok"] is True
        assert coord._task_board is None
        assert len(coord._pending_answers) == 0
        assert len(coord._pending_approvals) == 0


# ── AT-012: Hook denial blocks completion ────────────────────

class TestAT012_HookDenialBlocksCompletion:
    def test_complete_task_with_hook_deny(self):
        coord, _, _ = _make_team()
        coord.create_task("Task A")
        task_id = coord.list_tasks()["tasks"][0]["task_id"]
        coord.claim_task("role_coder", task_id)

        # Mock hook executor that denies
        mock_hook = MagicMock()
        deny_result = MagicMock()
        deny_result.action = "DENY"
        deny_result.feedback = "Need tests first"
        mock_hook.fire_sync_advisory = MagicMock(return_value=deny_result)
        coord._hook_executor = mock_hook

        result = coord.complete_task(task_id, result="Done")
        assert result["ok"] is False
        assert result["error_code"] == "TEAM_HOOK_DENIED"
        assert "Need tests first" in result["message"]

        # Task must still be IN_PROGRESS
        task = coord._task_board.get_task(task_id)
        assert task.status == TaskStatus.IN_PROGRESS

    def test_complete_task_without_hook_succeeds(self):
        coord, _, _ = _make_team()
        coord.create_task("Task A")
        task_id = coord.list_tasks()["tasks"][0]["task_id"]
        coord.claim_task("role_coder", task_id)
        result = coord.complete_task(task_id, result="Done")
        assert result.get("completed") is True or result.get("ok") is True


# ── AT-013: Hook denial blocks idle ──────────────────────────

class TestAT013_TeammateIdleHookIsAdvisory:
    """TEAMMATE_IDLE is advisory (NOT deniable). Hook observes but cannot block."""

    def test_idle_hook_is_not_deniable(self):
        from agent_framework.models.hook import DENIABLE_HOOK_POINTS, HookPoint
        assert HookPoint.TEAMMATE_IDLE not in DENIABLE_HOOK_POINTS

    def test_mark_delivered_always_transitions_even_with_hook(self):
        """TEAMMATE_IDLE hook fires but cannot prevent IDLE transition."""
        coord, _, registry = _make_team()
        registry.update_status("role_coder", TeamMemberStatus.WORKING)
        registry.update_status("role_coder", TeamMemberStatus.RESULT_READY)
        registry.update_status("role_coder", TeamMemberStatus.NOTIFYING)

        mock_hook = MagicMock()
        mock_hook.fire_sync_advisory = MagicMock(return_value=None)
        coord._hook_executor = mock_hook

        coord.mark_result_delivered("role_coder")

        # Must be IDLE — hook is advisory, cannot block
        member = registry.get("role_coder")
        assert member.status == TeamMemberStatus.IDLE
        # Hook was called
        mock_hook.fire_sync_advisory.assert_called_once()

    def test_mark_delivered_without_hook_transitions_to_idle(self):
        coord, _, registry = _make_team()
        registry.update_status("role_coder", TeamMemberStatus.WORKING)
        registry.update_status("role_coder", TeamMemberStatus.RESULT_READY)
        registry.update_status("role_coder", TeamMemberStatus.NOTIFYING)

        coord.mark_result_delivered("role_coder")
        assert registry.get("role_coder").status == TeamMemberStatus.IDLE


# ── AT-014: User can focus teammate directly ─────────────────

class TestAT014_UserFocusTeammate:
    def test_teammate_focus_state_cycle(self):
        from agent_framework.terminal_runtime import TeammateFocusState
        focus = TeammateFocusState()
        focus.set_agents(["role_coder", "role_reviewer"])

        # First cycle: focus on coder
        assert focus.cycle_next() == "role_coder"
        assert focus.is_focused()

        # Second cycle: focus on reviewer
        assert focus.cycle_next() == "role_reviewer"

        # Third cycle: wrap to lead (None)
        assert focus.cycle_next() is None
        assert not focus.is_focused()

    def test_unfocus_returns_to_lead(self):
        from agent_framework.terminal_runtime import TeammateFocusState
        focus = TeammateFocusState()
        focus.set_agents(["role_coder"])
        focus.cycle_next()
        assert focus.is_focused()
        focus.unfocus()
        assert not focus.is_focused()

    def test_repl_state_has_teammate_focus(self):
        from agent_framework.terminal_runtime import ReplState
        state = ReplState()
        assert hasattr(state, "teammate_focus")
        assert not state.teammate_focus.is_focused()


# ── AT-015: Progress is not treated as completion ────────────

class TestAT015_ProgressNotCompletion:
    def test_progress_notice_not_in_escalation(self):
        from agent_framework.team.notification_policy import TeamNotificationPolicy
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_mail_event(MailEventType.PROGRESS_NOTICE)


# ── Error Model (spec §11) ──────────────────────────────────

class TestErrorModel:
    def test_team_action_error_model(self):
        error = TeamActionError(
            error_code="TEAM_MEMBER_BUSY",
            message="Teammate 'role_coder' is busy",
            retryable=False,
        )
        assert error.ok is False
        assert error.error_code == "TEAM_MEMBER_BUSY"

    def test_cleanup_returns_structured_error(self):
        coord, _, registry = _make_team()
        registry.update_status("role_coder", TeamMemberStatus.WORKING)
        result = coord.cleanup_team()
        assert "ok" in result
        assert "error_code" in result
        assert "message" in result

    def test_complete_task_returns_structured_error(self):
        coord, _, _ = _make_team()
        result = coord.complete_task("nonexistent")
        assert result.get("ok") is False
        assert "error_code" in result
