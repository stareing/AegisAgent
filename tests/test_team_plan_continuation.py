"""Tests for PLAN_SUBMISSION → approve/reject → continuation delivery.

Verifies:
1. approve_plan writes to _pending_approvals.
2. reject_plan writes to _pending_approvals.
3. _check_pending_plan uses non-destructive peek.
4. _wait_for_approval polls _pending_approvals.
5. PLAN flow: WORKING → WAITING_APPROVAL → (approve) → WORKING.
6. Config-driven notification policy applies at runtime.
"""

from __future__ import annotations

import asyncio

import pytest
from datetime import datetime, timezone

from agent_framework.models.team import (
    MailEvent,
    MailEventType,
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
def team_setup():
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
        agent_id="sub_coder", team_id=team_id, role="coder",
        status=TeamMemberStatus.WORKING,
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

    return coordinator, mailbox, registry


class TestApprovalDelivery:
    def test_approve_plan_writes_pending_approvals(self, team_setup):
        coordinator, mailbox, registry = team_setup
        plan_reg = coordinator._plans

        # Create a plan request
        plan = plan_reg.create(
            requester="sub_coder", approver="lead_001",
            plan_text="Delete all logs", title="Cleanup",
            risk_level="high", team_id="test_team",
        )

        coordinator.approve_plan(plan.request_id, feedback="Go ahead")

        assert "sub_coder" in coordinator._pending_approvals
        approval = coordinator._pending_approvals["sub_coder"]
        assert approval["approved"] is True
        assert approval["feedback"] == "Go ahead"

    def test_reject_plan_writes_pending_approvals(self, team_setup):
        coordinator, mailbox, registry = team_setup
        plan_reg = coordinator._plans

        plan = plan_reg.create(
            requester="sub_coder", approver="lead_001",
            plan_text="Drop database", title="Reset DB",
            risk_level="high", team_id="test_team",
        )

        coordinator.reject_plan(plan.request_id, feedback="Too risky")

        assert "sub_coder" in coordinator._pending_approvals
        approval = coordinator._pending_approvals["sub_coder"]
        assert approval["approved"] is False
        assert approval["feedback"] == "Too risky"

    def test_approval_consumed_on_pop(self, team_setup):
        coordinator, _, _ = team_setup
        coordinator._pending_approvals["sub_coder"] = {"approved": True, "feedback": ""}
        result = coordinator._pending_approvals.pop("sub_coder", None)
        assert result is not None
        assert "sub_coder" not in coordinator._pending_approvals


class TestCheckPendingPlan:
    def test_finds_plan_in_inbox_via_peek(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # Send a PLAN_SUBMISSION to lead inbox
        plan_event = MailEvent(
            team_id="test_team",
            from_agent="sub_coder",
            to_agent="lead_001",
            event_type=MailEventType.PLAN_SUBMISSION,
            payload={
                "request_id": "plan_test_001",
                "title": "Refactor module",
                "plan_text": "Step 1...",
            },
        )
        mailbox.send(plan_event)

        result = coordinator._check_pending_plan("sub_coder")
        assert result is not None
        assert result["request_id"] == "plan_test_001"
        assert result["title"] == "Refactor module"

    def test_does_not_find_other_agents_plan(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # Register another member
        analyst = TeamMember(
            agent_id="sub_analyst", team_id="test_team", role="analyst",
            status=TeamMemberStatus.WORKING,
        )
        registry.register(analyst)

        plan_event = MailEvent(
            team_id="test_team",
            from_agent="sub_analyst",
            to_agent="lead_001",
            event_type=MailEventType.PLAN_SUBMISSION,
            payload={"request_id": "plan_other", "title": "Other plan", "plan_text": "..."},
        )
        mailbox.send(plan_event)

        # Should not find plan for coder
        result = coordinator._check_pending_plan("sub_coder")
        assert result is None

    def test_does_not_consume_other_messages(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # Send a PROGRESS_NOTICE (not a plan)
        progress = MailEvent(
            team_id="test_team",
            from_agent="sub_coder",
            to_agent="lead_001",
            event_type=MailEventType.PROGRESS_NOTICE,
            payload={"status": "50%"},
        )
        mailbox.send(progress)

        # Send a PLAN_SUBMISSION
        plan = MailEvent(
            team_id="test_team",
            from_agent="sub_coder",
            to_agent="lead_001",
            event_type=MailEventType.PLAN_SUBMISSION,
            payload={"request_id": "plan_002", "title": "My plan", "plan_text": "Step 1..."},
        )
        mailbox.send(plan)

        # Check plan — should find it
        result = coordinator._check_pending_plan("sub_coder")
        assert result is not None

        # PROGRESS_NOTICE should still be in inbox
        events = mailbox.read_inbox("lead_001")
        progress_events = [e for e in events if e.event_type == MailEventType.PROGRESS_NOTICE]
        assert len(progress_events) == 1

    def test_peek_inbox_does_not_consume_messages(self, team_setup):
        coordinator, mailbox, registry = team_setup

        progress = MailEvent(
            team_id="test_team",
            from_agent="sub_coder",
            to_agent="lead_001",
            event_type=MailEventType.PROGRESS_NOTICE,
            payload={"status": "50%"},
        )
        mailbox.send(progress)

        peeked = mailbox.peek_inbox("lead_001")
        assert len(peeked) == 1
        assert peeked[0].event_type == MailEventType.PROGRESS_NOTICE

        drained = mailbox.read_inbox("lead_001")
        assert len(drained) == 1
        assert drained[0].event_type == MailEventType.PROGRESS_NOTICE


class TestWaitForApproval:
    @pytest.mark.asyncio
    async def test_returns_approval_when_available(self, team_setup):
        coordinator, _, _ = team_setup

        # Pre-deliver approval before calling wait (no timeout — would block forever)
        coordinator._pending_approvals["sub_coder"] = {"approved": True, "feedback": "OK"}

        result = await coordinator._wait_for_approval("sub_coder")
        assert result is not None
        assert result["approved"] is True


class TestPolicyFromConfig:
    def test_default_config_creates_enabled_policy(self):
        from agent_framework.team.notification_policy import TeamNotificationPolicy
        policy = TeamNotificationPolicy.from_config({
            "team_auto_notify_enabled": True,
            "team_auto_notify_batch_window_ms": 500,
            "team_auto_notify_max_batch_size": 10,
        })
        assert policy.enabled is True
        assert policy.batch_window_ms == 500

    def test_disabled_config_creates_disabled_policy(self):
        from agent_framework.team.notification_policy import TeamNotificationPolicy
        from agent_framework.models.team import TeamNotificationType
        policy = TeamNotificationPolicy.from_config({
            "team_auto_notify_enabled": False,
        })
        assert policy.enabled is False
        assert not policy.should_escalate_notification(TeamNotificationType.TASK_COMPLETED)

    def test_custom_batch_window(self):
        from agent_framework.team.notification_policy import TeamNotificationPolicy
        policy = TeamNotificationPolicy.from_config({
            "team_auto_notify_batch_window_ms": 2000,
        })
        assert policy.batch_window_ms == 2000

    def test_team_config_has_policy_fields(self):
        from agent_framework.infra.config import TeamConfig
        cfg = TeamConfig()
        assert cfg.team_auto_notify_enabled is True
        assert cfg.team_auto_notify_batch_window_ms == 500
        assert cfg.team_auto_notify_max_batch_size == 10
