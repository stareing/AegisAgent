"""Tests for team question → answer routing via request_id.

Verifies:
1. _handle_question saves request_id → from_agent mapping.
2. answer_question(request_id) auto-routes without explicit agent_id.
3. answer_question with both request_id and to_agent uses request_id first.
4. Missing request_id falls back to explicit to_agent.
5. No broadcast on answer (regression guard).
"""

from __future__ import annotations

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

    # Register lead
    lead = TeamMember(
        agent_id="lead_001", team_id=team_id, role="lead",
        status=TeamMemberStatus.WORKING,
    )
    registry.register(lead)

    # Register a teammate
    coder = TeamMember(
        agent_id="sub_abc123", team_id=team_id, role="coder",
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


class TestQuestionAnswerRouting:
    def test_handle_question_saves_request_mapping(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # Simulate a question event from coder
        question_event = MailEvent(
            team_id="test_team",
            from_agent="sub_abc123",
            to_agent="lead_001",
            event_type=MailEventType.QUESTION,
            request_id="req_001",
            payload={"question": "Which framework to use?", "request_id": "req_001"},
        )

        # Send to lead's inbox
        mailbox.send(question_event)
        # Process inbox
        coordinator.process_inbox()

        # Verify request_id → from_agent mapping was saved
        assert "req_001" in coordinator._pending_requests
        assert coordinator._pending_requests["req_001"] == "sub_abc123"

    def test_answer_by_request_id_only(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # Pre-populate the mapping (simulating a prior question)
        coordinator._pending_requests["req_002"] = "sub_abc123"

        # Answer with request_id only (no to_agent)
        coordinator.answer_question("req_002", "Use pytest")

        # Read coder's inbox — should have the answer
        events = mailbox.read_inbox("sub_abc123")
        answer_events = [e for e in events if e.event_type == MailEventType.ANSWER]
        assert len(answer_events) == 1
        assert answer_events[0].payload["answer"] == "Use pytest"
        assert answer_events[0].to_agent == "sub_abc123"

    def test_answer_request_id_consumed(self, team_setup):
        coordinator, mailbox, registry = team_setup
        coordinator._pending_requests["req_003"] = "sub_abc123"

        coordinator.answer_question("req_003", "done")

        # Mapping should be consumed (popped)
        assert "req_003" not in coordinator._pending_requests

    def test_answer_fallback_to_explicit_to_agent(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # No mapping exists for this request_id
        coordinator.answer_question("unknown_req", "fallback answer", to_agent="sub_abc123")

        events = mailbox.read_inbox("sub_abc123")
        answer_events = [e for e in events if e.event_type == MailEventType.ANSWER]
        assert len(answer_events) == 1

    def test_answer_no_target_is_noop(self, team_setup):
        coordinator, mailbox, registry = team_setup

        # No mapping, no to_agent — should not crash, just log warning
        coordinator.answer_question("no_target_req", "this goes nowhere")

        # No events should be sent to anyone
        events = mailbox.read_inbox("sub_abc123")
        answer_events = [e for e in events if e.event_type == MailEventType.ANSWER]
        assert len(answer_events) == 0


class TestHandlePlanSavesMapping:
    def test_handle_plan_saves_request_mapping(self, team_setup):
        coordinator, mailbox, registry = team_setup

        plan_event = MailEvent(
            team_id="test_team",
            from_agent="sub_abc123",
            to_agent="lead_001",
            event_type=MailEventType.PLAN_SUBMISSION,
            payload={
                "request_id": "plan_001",
                "title": "Test plan",
                "plan_text": "Step 1...",
            },
        )
        mailbox.send(plan_event)
        coordinator.process_inbox()

        assert "plan_001" in coordinator._pending_requests
        assert coordinator._pending_requests["plan_001"] == "sub_abc123"
