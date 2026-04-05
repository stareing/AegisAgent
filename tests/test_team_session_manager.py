"""Tests for TeamSessionManager — persistent teammate sessions (AT-008).

Verifies:
1. create_session returns valid session with session_id.
2. get_session returns existing session.
3. get_or_create_session creates if missing.
4. update_session tracks run_id, task_id, status.
5. end_session removes session.
6. list_sessions returns all active.
7. clear removes all.
8. Coordinator integrates session manager.
9. Session survives across multiple runs (same session_id, different run_ids).
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from agent_framework.models.team import TeamMemberStatus, TeamSessionState
from agent_framework.team.session_manager import TeamSessionManager


@pytest.fixture
def mgr() -> TeamSessionManager:
    return TeamSessionManager("team_test")


class TestCreateSession:
    def test_returns_session_with_id(self, mgr: TeamSessionManager):
        session = mgr.create_session("role_coder")
        assert session.session_id.startswith("sess_")
        assert session.member_id == "role_coder"
        assert session.team_id == "team_test"
        assert session.status == TeamMemberStatus.IDLE

    def test_replaces_existing(self, mgr: TeamSessionManager):
        s1 = mgr.create_session("role_coder")
        s2 = mgr.create_session("role_coder")
        assert s1.session_id != s2.session_id
        assert mgr.get_session("role_coder").session_id == s2.session_id


class TestGetSession:
    def test_returns_none_for_missing(self, mgr: TeamSessionManager):
        assert mgr.get_session("nonexistent") is None

    def test_returns_existing(self, mgr: TeamSessionManager):
        mgr.create_session("role_coder")
        assert mgr.get_session("role_coder") is not None


class TestGetOrCreate:
    def test_creates_if_missing(self, mgr: TeamSessionManager):
        session = mgr.get_or_create_session("role_reviewer")
        assert session.member_id == "role_reviewer"

    def test_returns_existing_if_present(self, mgr: TeamSessionManager):
        s1 = mgr.create_session("role_coder")
        s2 = mgr.get_or_create_session("role_coder")
        assert s1.session_id == s2.session_id


class TestUpdateSession:
    def test_updates_run_id(self, mgr: TeamSessionManager):
        mgr.create_session("role_coder")
        mgr.update_session("role_coder", run_id="run_001")
        assert mgr.get_session("role_coder").last_run_id == "run_001"

    def test_updates_task_id(self, mgr: TeamSessionManager):
        mgr.create_session("role_coder")
        mgr.update_session("role_coder", task_id="task_abc")
        assert mgr.get_session("role_coder").current_task_id == "task_abc"

    def test_updates_status(self, mgr: TeamSessionManager):
        mgr.create_session("role_coder")
        mgr.update_session("role_coder", status=TeamMemberStatus.WORKING)
        assert mgr.get_session("role_coder").status == TeamMemberStatus.WORKING

    def test_preserves_session_id_across_updates(self, mgr: TeamSessionManager):
        s = mgr.create_session("role_coder")
        sid = s.session_id
        mgr.update_session("role_coder", run_id="run_001")
        mgr.update_session("role_coder", run_id="run_002")
        assert mgr.get_session("role_coder").session_id == sid

    def test_returns_none_for_missing(self, mgr: TeamSessionManager):
        assert mgr.update_session("nonexistent", run_id="x") is None


class TestEndSession:
    def test_removes_session(self, mgr: TeamSessionManager):
        mgr.create_session("role_coder")
        assert mgr.end_session("role_coder") is True
        assert mgr.get_session("role_coder") is None

    def test_returns_false_for_missing(self, mgr: TeamSessionManager):
        assert mgr.end_session("nonexistent") is False


class TestListAndClear:
    def test_list_sessions(self, mgr: TeamSessionManager):
        mgr.create_session("a")
        mgr.create_session("b")
        assert len(mgr.list_sessions()) == 2

    def test_clear(self, mgr: TeamSessionManager):
        mgr.create_session("a")
        mgr.create_session("b")
        mgr.clear()
        assert len(mgr.list_sessions()) == 0

    def test_has_session(self, mgr: TeamSessionManager):
        assert not mgr.has_session("a")
        mgr.create_session("a")
        assert mgr.has_session("a")


class TestCoordinatorIntegration:
    def test_coordinator_has_session_manager(self):
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.coordinator import TeamCoordinator
        from agent_framework.team.mailbox import TeamMailbox
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.registry import TeamRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry

        bus = AgentBus(persistence=InMemoryBusPersistence())
        registry = TeamRegistry("t")
        mailbox = TeamMailbox(bus, registry)
        coord = TeamCoordinator("t", "lead", mailbox, registry,
                                PlanRegistry(), ShutdownRegistry())
        assert hasattr(coord, "_session_manager")
        assert coord._session_manager is not None

    def test_status_includes_session_count(self):
        from agent_framework.models.team import TeamMember, TeamMemberStatus
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.coordinator import TeamCoordinator
        from agent_framework.team.mailbox import TeamMailbox
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.registry import TeamRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry

        bus = AgentBus(persistence=InMemoryBusPersistence())
        registry = TeamRegistry("t")
        mailbox = TeamMailbox(bus, registry)
        registry.register(TeamMember(agent_id="lead", team_id="t", role="lead",
                                     status=TeamMemberStatus.WORKING))
        coord = TeamCoordinator("t", "lead", mailbox, registry,
                                PlanRegistry(), ShutdownRegistry())
        status = coord.get_team_status(caller_id="lead")
        assert "active_sessions" in status


class TestSessionPersistsAcrossRuns:
    """Same session_id across multiple run_ids (AT-008 core requirement)."""

    def test_same_session_different_runs(self, mgr: TeamSessionManager):
        session = mgr.create_session("role_coder")
        original_sid = session.session_id

        # First run
        mgr.update_session("role_coder", run_id="run_001",
                           status=TeamMemberStatus.WORKING)
        assert mgr.get_session("role_coder").last_run_id == "run_001"
        assert mgr.get_session("role_coder").session_id == original_sid

        # Q&A continuation — new run, same session
        mgr.update_session("role_coder", run_id="run_002")
        assert mgr.get_session("role_coder").last_run_id == "run_002"
        assert mgr.get_session("role_coder").session_id == original_sid

        # Second task — new run, same session
        mgr.update_session("role_coder", run_id="run_003",
                           task_id="task_new")
        assert mgr.get_session("role_coder").last_run_id == "run_003"
        assert mgr.get_session("role_coder").session_id == original_sid
