"""Tests for TeamRegistry, PlanRegistry, and ShutdownRegistry."""

from __future__ import annotations

import pytest

from agent_framework.models.team import (
    TeamMember,
    TeamMemberStatus,
    PlanStatus,
    ShutdownStatus,
)
from agent_framework.team.registry import TeamRegistry, TerminalStatusError
from agent_framework.team.plan_registry import PlanRegistry, TerminalPlanStatusError
from agent_framework.team.shutdown_registry import (
    ShutdownRegistry,
    TerminalShutdownStatusError,
)


# ---------------------------------------------------------------------------
# TeamRegistry
# ---------------------------------------------------------------------------

class TestTeamRegistry:
    def test_register_and_get(self) -> None:
        reg = TeamRegistry()
        member = TeamMember(agent_id="a1", role="lead")
        reg.register(member)
        assert reg.get("a1") is not None
        assert reg.get("a1").role == "lead"

    def test_get_missing_returns_none(self) -> None:
        reg = TeamRegistry()
        assert reg.get("nonexistent") is None

    def test_list_members_all(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1"))
        reg.register(TeamMember(agent_id="a2", status=TeamMemberStatus.WORKING))
        assert len(reg.list_members()) == 2

    def test_list_members_filtered(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1", status=TeamMemberStatus.IDLE))
        reg.register(TeamMember(agent_id="a2", status=TeamMemberStatus.WORKING))
        idle = reg.list_members(status=TeamMemberStatus.IDLE)
        assert len(idle) == 1
        assert idle[0].agent_id == "a1"

    def test_update_status(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1"))
        reg.update_status("a1", TeamMemberStatus.WORKING)
        member = reg.get("a1")
        assert member.status == TeamMemberStatus.WORKING

    def test_update_status_missing_raises(self) -> None:
        reg = TeamRegistry()
        with pytest.raises(KeyError):
            reg.update_status("missing", TeamMemberStatus.WORKING)

    def test_terminal_status_shutdown_blocks_transition(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1"))
        reg.update_status("a1", TeamMemberStatus.SHUTDOWN)
        with pytest.raises(TerminalStatusError):
            reg.update_status("a1", TeamMemberStatus.IDLE)

    def test_terminal_status_failed_blocks_transition(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1"))
        reg.update_status("a1", TeamMemberStatus.FAILED)
        with pytest.raises(TerminalStatusError):
            reg.update_status("a1", TeamMemberStatus.WORKING)

    def test_remove(self) -> None:
        reg = TeamRegistry()
        reg.register(TeamMember(agent_id="a1"))
        reg.remove("a1")
        assert reg.get("a1") is None

    def test_remove_missing_is_noop(self) -> None:
        reg = TeamRegistry()
        reg.remove("nonexistent")  # should not raise

    def test_get_team_id(self) -> None:
        reg = TeamRegistry(team_id="team-42")
        assert reg.get_team_id() == "team-42"

    def test_get_team_id_auto_generated(self) -> None:
        reg = TeamRegistry()
        assert len(reg.get_team_id()) > 0


# ---------------------------------------------------------------------------
# PlanRegistry
# ---------------------------------------------------------------------------

class TestPlanRegistry:
    def test_create_and_get(self) -> None:
        reg = PlanRegistry()
        plan = reg.create(requester="a1", approver="lead", plan_text="do stuff")
        fetched = reg.get(plan.request_id)
        assert fetched is not None
        assert fetched.status == PlanStatus.PENDING
        assert fetched.plan_text == "do stuff"

    def test_approve(self) -> None:
        reg = PlanRegistry()
        plan = reg.create(requester="a1", approver="lead", plan_text="p")
        approved = reg.approve(plan.request_id, feedback="lgtm")
        assert approved.status == PlanStatus.APPROVED
        assert approved.feedback == "lgtm"

    def test_reject(self) -> None:
        reg = PlanRegistry()
        plan = reg.create(requester="a1", approver="lead", plan_text="p")
        rejected = reg.reject(plan.request_id, feedback="too risky")
        assert rejected.status == PlanStatus.REJECTED

    def test_terminal_approved_cannot_change(self) -> None:
        reg = PlanRegistry()
        plan = reg.create(requester="a1", approver="lead", plan_text="p")
        reg.approve(plan.request_id)
        with pytest.raises(TerminalPlanStatusError):
            reg.reject(plan.request_id)

    def test_terminal_rejected_cannot_change(self) -> None:
        reg = PlanRegistry()
        plan = reg.create(requester="a1", approver="lead", plan_text="p")
        reg.reject(plan.request_id)
        with pytest.raises(TerminalPlanStatusError):
            reg.approve(plan.request_id)

    def test_list_pending(self) -> None:
        reg = PlanRegistry()
        p1 = reg.create(requester="a1", approver="lead", plan_text="p1")
        p2 = reg.create(requester="a2", approver="lead", plan_text="p2")
        reg.approve(p1.request_id)
        pending = reg.list_pending()
        assert len(pending) == 1
        assert pending[0].request_id == p2.request_id

    def test_list_pending_filter_approver(self) -> None:
        reg = PlanRegistry()
        reg.create(requester="a1", approver="lead-A", plan_text="p1")
        reg.create(requester="a2", approver="lead-B", plan_text="p2")
        assert len(reg.list_pending(approver="lead-A")) == 1

    def test_get_missing_returns_none(self) -> None:
        reg = PlanRegistry()
        assert reg.get("nonexistent") is None

    def test_approve_missing_raises(self) -> None:
        reg = PlanRegistry()
        with pytest.raises(KeyError):
            reg.approve("nonexistent")


# ---------------------------------------------------------------------------
# ShutdownRegistry
# ---------------------------------------------------------------------------

class TestShutdownRegistry:
    def test_create_and_get(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1", reason="done")
        fetched = reg.get(req.request_id)
        assert fetched is not None
        assert fetched.status == ShutdownStatus.PENDING

    def test_acknowledge(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        acked = reg.acknowledge(req.request_id)
        assert acked.status == ShutdownStatus.ACKNOWLEDGED

    def test_complete(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        reg.acknowledge(req.request_id)
        completed = reg.complete(req.request_id)
        assert completed.status == ShutdownStatus.COMPLETED

    def test_reject(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        rejected = reg.reject(req.request_id)
        assert rejected.status == ShutdownStatus.REJECTED

    def test_timeout(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        timed_out = reg.timeout(req.request_id)
        assert timed_out.status == ShutdownStatus.TIMEOUT

    def test_terminal_completed_cannot_change(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        reg.acknowledge(req.request_id)
        reg.complete(req.request_id)
        with pytest.raises(TerminalShutdownStatusError):
            reg.reject(req.request_id)

    def test_terminal_rejected_cannot_change(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        reg.reject(req.request_id)
        with pytest.raises(TerminalShutdownStatusError):
            reg.acknowledge(req.request_id)

    def test_terminal_timeout_cannot_change(self) -> None:
        reg = ShutdownRegistry()
        req = reg.create(requester="lead", target="a1")
        reg.timeout(req.request_id)
        with pytest.raises(TerminalShutdownStatusError):
            reg.complete(req.request_id)

    def test_list_pending(self) -> None:
        reg = ShutdownRegistry()
        r1 = reg.create(requester="lead", target="a1")
        r2 = reg.create(requester="lead", target="a2")
        reg.complete(r1.request_id)
        pending = reg.list_pending()
        assert len(pending) == 1
        assert pending[0].request_id == r2.request_id

    def test_get_missing_returns_none(self) -> None:
        reg = ShutdownRegistry()
        assert reg.get("nonexistent") is None

    def test_complete_missing_raises(self) -> None:
        reg = ShutdownRegistry()
        with pytest.raises(KeyError):
            reg.complete("nonexistent")
