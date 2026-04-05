"""Tests for team member status transitions.

Verifies the state machine:
    IDLE → WORKING → RESULT_READY → NOTIFYING → IDLE
    WORKING → FAILED
    RESULT_READY → FAILED
    NOTIFYING → FAILED
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from agent_framework.models.team import TeamMember, TeamMemberStatus
from agent_framework.team.registry import TeamRegistry


@pytest.fixture
def registry() -> TeamRegistry:
    r = TeamRegistry("test_team")
    member = TeamMember(
        agent_id="sub_abc123",
        team_id="test_team",
        role="coder",
        status=TeamMemberStatus.IDLE,
        joined_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    r.register(member)
    return r


class TestTeamMemberStatusValues:
    """Verify all expected statuses exist in the enum."""

    def test_result_ready_exists(self):
        assert TeamMemberStatus.RESULT_READY == "RESULT_READY"

    def test_notifying_exists(self):
        assert TeamMemberStatus.NOTIFYING == "NOTIFYING"

    def test_all_statuses(self):
        expected = {
            "SPAWNING", "WORKING", "IDLE", "RESULT_READY", "NOTIFYING",
            "WAITING_APPROVAL", "WAITING_ANSWER",
            "SHUTDOWN_REQUESTED", "SHUTDOWN", "FAILED",
        }
        actual = {s.value for s in TeamMemberStatus}
        assert expected == actual


class TestStatusTransitions:
    """Test state machine transitions via TeamRegistry."""

    def test_idle_to_working(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.WORKING

    def test_working_to_result_ready(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.RESULT_READY

    def test_result_ready_to_notifying(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        registry.update_status("sub_abc123", TeamMemberStatus.NOTIFYING)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.NOTIFYING

    def test_notifying_to_idle(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        registry.update_status("sub_abc123", TeamMemberStatus.NOTIFYING)
        registry.update_status("sub_abc123", TeamMemberStatus.IDLE)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.IDLE

    def test_working_to_failed(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.FAILED)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.FAILED

    def test_result_ready_to_failed(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        registry.update_status("sub_abc123", TeamMemberStatus.FAILED)
        m = registry.get("sub_abc123")
        assert m.status == TeamMemberStatus.FAILED

    def test_full_lifecycle(self, registry: TeamRegistry):
        """Complete happy path: IDLE → WORKING → RESULT_READY → NOTIFYING → IDLE."""
        transitions = [
            TeamMemberStatus.WORKING,
            TeamMemberStatus.RESULT_READY,
            TeamMemberStatus.NOTIFYING,
            TeamMemberStatus.IDLE,
        ]
        for status in transitions:
            registry.update_status("sub_abc123", status)
            m = registry.get("sub_abc123")
            assert m.status == status


class TestCoordinatorMarkMethods:
    """Test TeamCoordinator.mark_result_* methods."""

    def _make_coordinator(self, registry: TeamRegistry):
        from agent_framework.team.coordinator import TeamCoordinator
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.mailbox import TeamMailbox

        bus = AgentBus(persistence=InMemoryBusPersistence())
        mailbox = TeamMailbox(bus, registry)
        return TeamCoordinator(
            team_id="test_team",
            lead_agent_id="lead_001",
            mailbox=mailbox,
            team_registry=registry,
            plan_registry=PlanRegistry(),
            shutdown_registry=ShutdownRegistry(),
        )

    def test_mark_result_notifying(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        coord = self._make_coordinator(registry)
        coord.mark_result_notifying("sub_abc123")
        assert registry.get("sub_abc123").status == TeamMemberStatus.NOTIFYING

    def test_mark_result_notifying_ignores_non_result_ready(self, registry: TeamRegistry):
        # WORKING → mark_result_notifying should be no-op
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        coord = self._make_coordinator(registry)
        coord.mark_result_notifying("sub_abc123")
        assert registry.get("sub_abc123").status == TeamMemberStatus.WORKING

    def test_mark_result_delivered(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.WORKING)
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        registry.update_status("sub_abc123", TeamMemberStatus.NOTIFYING)
        coord = self._make_coordinator(registry)
        coord.mark_result_delivered("sub_abc123")
        assert registry.get("sub_abc123").status == TeamMemberStatus.IDLE

    def test_mark_result_delivery_failed(self, registry: TeamRegistry):
        registry.update_status("sub_abc123", TeamMemberStatus.RESULT_READY)
        coord = self._make_coordinator(registry)
        coord.mark_result_delivery_failed("sub_abc123", "test failure")
        assert registry.get("sub_abc123").status == TeamMemberStatus.FAILED
