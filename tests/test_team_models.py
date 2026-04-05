"""Tests for agent_framework.models.team — Team data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent_framework.models.subagent import AckLevel
from agent_framework.models.team import (
    EVENT_PRIORITY,
    PAYLOAD_VALIDATORS,
    TERMINAL_MEMBER_STATUSES,
    TERMINAL_PLAN_STATUSES,
    TERMINAL_SHUTDOWN_STATUSES,
    AnswerPayload,
    ApprovalPayload,
    MailEvent,
    MailEventType,
    PlanRequest,
    PlanStatus,
    PlanSubmissionPayload,
    QuestionPayload,
    ShutdownAckPayload,
    ShutdownRequest,
    ShutdownRequestPayload,
    ShutdownStatus,
    TeamMember,
    TeamMemberStatus,
)


# -----------------------------------------------------------------------
# MailEventType
# -----------------------------------------------------------------------

class TestMailEventType:
    """MailEventType enum has exactly 16 members."""

    def test_member_count(self) -> None:
        assert len(MailEventType) == 16

    def test_all_expected_members(self) -> None:
        expected = {
            "TASK_ASSIGNMENT", "TASK_CLAIM_REQUEST", "TASK_CLAIMED_NOTICE",
            "TASK_HANDOFF_REQUEST", "TASK_HANDOFF_RESPONSE",
            "PLAN_SUBMISSION", "APPROVAL_RESPONSE",
            "QUESTION", "ANSWER",
            "PROGRESS_NOTICE", "STATUS_PING", "STATUS_REPLY",
            "SHUTDOWN_REQUEST", "SHUTDOWN_ACK",
            "ERROR_NOTICE", "BROADCAST_NOTICE",
        }
        assert {m.value for m in MailEventType} == expected

    def test_is_str_enum(self) -> None:
        assert isinstance(MailEventType.QUESTION, str)
        assert MailEventType.QUESTION == "QUESTION"


# -----------------------------------------------------------------------
# MailEvent — serialization / deserialization / immutability
# -----------------------------------------------------------------------

class TestMailEvent:
    """MailEvent frozen model round-trip and immutability."""

    def _make_event(self, **overrides: object) -> MailEvent:
        defaults = {
            "event_id": "evt_001",
            "team_id": "team_abc",
            "from_agent": "lead",
            "to_agent": "coder_1",
            "event_type": MailEventType.TASK_ASSIGNMENT,
            "payload": {"task_id": 42},
        }
        defaults.update(overrides)
        return MailEvent(**defaults)

    def test_round_trip_json(self) -> None:
        event = self._make_event()
        data = event.model_dump(mode="json")
        restored = MailEvent.model_validate(data)
        assert restored == event
        assert restored.event_type == MailEventType.TASK_ASSIGNMENT

    def test_defaults(self) -> None:
        event = MailEvent(event_type=MailEventType.STATUS_PING)
        assert event.event_id == ""
        assert event.schema_version == "1.1"
        assert event.ack_level == AckLevel.NONE
        assert event.requires_ack is False
        assert event.payload == {}
        assert event.request_id is None
        assert event.correlation_id is None

    def test_frozen_raises_on_attribute_set(self) -> None:
        event = self._make_event()
        with pytest.raises(ValidationError):
            event.event_id = "changed"  # type: ignore[misc]

    def test_frozen_raises_on_payload_reassign(self) -> None:
        event = self._make_event()
        with pytest.raises(ValidationError):
            event.payload = {"new": True}  # type: ignore[misc]

    def test_broadcast_marker(self) -> None:
        event = self._make_event(to_agent="*")
        assert event.to_agent == "*"

    def test_ack_level_from_subagent(self) -> None:
        event = self._make_event(ack_level=AckLevel.HANDLED)
        assert event.ack_level == AckLevel.HANDLED

    def test_created_at_auto(self) -> None:
        event = MailEvent(event_type=MailEventType.ANSWER)
        assert isinstance(event.created_at, datetime)


# -----------------------------------------------------------------------
# TeamMemberStatus
# -----------------------------------------------------------------------

class TestTeamMemberStatus:
    def test_member_count(self) -> None:
        assert len(TeamMemberStatus) == 10

    def test_terminal_statuses(self) -> None:
        assert TeamMemberStatus.SHUTDOWN in TERMINAL_MEMBER_STATUSES
        assert TeamMemberStatus.FAILED in TERMINAL_MEMBER_STATUSES
        assert TeamMemberStatus.WORKING not in TERMINAL_MEMBER_STATUSES


# -----------------------------------------------------------------------
# TeamMember
# -----------------------------------------------------------------------

class TestTeamMember:
    def test_defaults(self) -> None:
        m = TeamMember(agent_id="a1", team_id="t1")
        assert m.role == "teammate"
        assert m.status == TeamMemberStatus.SPAWNING
        assert m.spawn_id == ""
        assert m.active_task_ids == []

    def test_round_trip(self) -> None:
        m = TeamMember(
            agent_id="a1", team_id="t1", role="lead",
            status=TeamMemberStatus.WORKING,
            active_task_ids=[1, 2, 3],
        )
        data = m.model_dump(mode="json")
        restored = TeamMember.model_validate(data)
        assert restored.agent_id == "a1"
        assert restored.active_task_ids == [1, 2, 3]

    def test_mutable_status(self) -> None:
        m = TeamMember(agent_id="a1", team_id="t1")
        m.status = TeamMemberStatus.WORKING
        assert m.status == TeamMemberStatus.WORKING


# -----------------------------------------------------------------------
# PlanStatus + PlanRequest
# -----------------------------------------------------------------------

class TestPlanRequest:
    def test_plan_statuses(self) -> None:
        assert PlanStatus.PENDING not in TERMINAL_PLAN_STATUSES
        assert PlanStatus.APPROVED in TERMINAL_PLAN_STATUSES
        assert PlanStatus.REJECTED in TERMINAL_PLAN_STATUSES

    def test_round_trip(self) -> None:
        p = PlanRequest(
            request_id="req_1", requester="coder", approver="lead",
            plan_text="Refactor module X", title="Refactor",
            risk_level="medium", team_id="t1",
        )
        data = p.model_dump(mode="json")
        restored = PlanRequest.model_validate(data)
        assert restored.request_id == "req_1"
        assert restored.status == PlanStatus.PENDING

    def test_frozen(self) -> None:
        p = PlanRequest(
            request_id="req_1", requester="c", approver="l",
            plan_text="plan",
        )
        with pytest.raises(ValidationError):
            p.status = PlanStatus.APPROVED  # type: ignore[misc]

    def test_model_copy_works(self) -> None:
        """Registries use model_copy to transition frozen models."""
        p = PlanRequest(
            request_id="req_1", requester="c", approver="l",
            plan_text="plan",
        )
        updated = p.model_copy(update={"status": PlanStatus.APPROVED})
        assert updated.status == PlanStatus.APPROVED
        assert p.status == PlanStatus.PENDING


# -----------------------------------------------------------------------
# ShutdownStatus + ShutdownRequest
# -----------------------------------------------------------------------

class TestShutdownRequest:
    def test_shutdown_statuses(self) -> None:
        assert ShutdownStatus.PENDING not in TERMINAL_SHUTDOWN_STATUSES
        assert ShutdownStatus.COMPLETED in TERMINAL_SHUTDOWN_STATUSES
        assert ShutdownStatus.TIMEOUT in TERMINAL_SHUTDOWN_STATUSES

    def test_round_trip(self) -> None:
        s = ShutdownRequest(
            request_id="sd_1", requester="lead", target="coder_1",
            reason="task done", team_id="t1",
        )
        data = s.model_dump(mode="json")
        restored = ShutdownRequest.model_validate(data)
        assert restored.target == "coder_1"

    def test_frozen(self) -> None:
        s = ShutdownRequest(
            request_id="sd_1", requester="lead", target="coder_1",
        )
        with pytest.raises(ValidationError):
            s.status = ShutdownStatus.COMPLETED  # type: ignore[misc]


# -----------------------------------------------------------------------
# Typed Payloads — valid + invalid
# -----------------------------------------------------------------------

class TestQuestionPayload:
    def test_valid(self) -> None:
        p = QuestionPayload(
            request_id="r1", question="Which framework?",
            options=["A", "B"], suggested_default="A",
        )
        assert p.question == "Which framework?"
        assert p.options == ["A", "B"]

    def test_minimal(self) -> None:
        p = QuestionPayload(request_id="r1", question="Why?")
        assert p.task_id is None
        assert p.options == []

    def test_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            QuestionPayload(request_id="r1")  # type: ignore[call-arg]


class TestAnswerPayload:
    def test_valid(self) -> None:
        p = AnswerPayload(request_id="r1", answer="Use pytest")
        assert p.answer == "Use pytest"

    def test_missing_answer(self) -> None:
        with pytest.raises(ValidationError):
            AnswerPayload(request_id="r1")  # type: ignore[call-arg]


class TestPlanSubmissionPayload:
    def test_valid(self) -> None:
        p = PlanSubmissionPayload(
            request_id="r1", title="Refactor", plan_text="Step 1...",
        )
        assert p.risk_level == "low"

    def test_missing_title(self) -> None:
        with pytest.raises(ValidationError):
            PlanSubmissionPayload(request_id="r1", plan_text="x")  # type: ignore[call-arg]


class TestApprovalPayload:
    def test_approved(self) -> None:
        p = ApprovalPayload(request_id="r1", approved=True, feedback="LGTM")
        assert p.approved is True

    def test_rejected(self) -> None:
        p = ApprovalPayload(request_id="r1", approved=False)
        assert p.feedback == ""

    def test_missing_approved(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalPayload(request_id="r1")  # type: ignore[call-arg]


class TestShutdownRequestPayload:
    def test_valid(self) -> None:
        p = ShutdownRequestPayload(request_id="r1", reason="done")
        assert p.reason == "done"

    def test_defaults(self) -> None:
        p = ShutdownRequestPayload(request_id="r1")
        assert p.reason == ""


class TestShutdownAckPayload:
    def test_valid(self) -> None:
        p = ShutdownAckPayload(request_id="r1", accepted=True)
        assert p.accepted is True

    def test_missing_accepted(self) -> None:
        with pytest.raises(ValidationError):
            ShutdownAckPayload(request_id="r1")  # type: ignore[call-arg]


# -----------------------------------------------------------------------
# PAYLOAD_VALIDATORS mapping
# -----------------------------------------------------------------------

class TestPayloadValidators:
    def test_mapping_count(self) -> None:
        assert len(PAYLOAD_VALIDATORS) == 6

    def test_question_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.QUESTION] is QuestionPayload

    def test_answer_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.ANSWER] is AnswerPayload

    def test_plan_submission_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.PLAN_SUBMISSION] is PlanSubmissionPayload

    def test_approval_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.APPROVAL_RESPONSE] is ApprovalPayload

    def test_shutdown_request_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.SHUTDOWN_REQUEST] is ShutdownRequestPayload

    def test_shutdown_ack_maps_correctly(self) -> None:
        assert PAYLOAD_VALIDATORS[MailEventType.SHUTDOWN_ACK] is ShutdownAckPayload

    def test_validate_valid_payload(self) -> None:
        """Validator accepts well-formed payload dict."""
        raw = {"request_id": "r1", "question": "Why?"}
        model_cls = PAYLOAD_VALIDATORS[MailEventType.QUESTION]
        parsed = model_cls.model_validate(raw)
        assert isinstance(parsed, QuestionPayload)

    def test_validate_invalid_payload(self) -> None:
        """Validator rejects malformed payload dict."""
        raw = {"wrong_field": "value"}
        model_cls = PAYLOAD_VALIDATORS[MailEventType.QUESTION]
        with pytest.raises(ValidationError):
            model_cls.model_validate(raw)


# -----------------------------------------------------------------------
# EVENT_PRIORITY completeness
# -----------------------------------------------------------------------

class TestEventPriority:
    def test_covers_all_event_types(self) -> None:
        """Every MailEventType must have a priority entry."""
        missing = set(MailEventType) - set(EVENT_PRIORITY.keys())
        assert missing == set(), f"Missing priority for: {missing}"

    def test_shutdown_request_is_highest(self) -> None:
        assert EVENT_PRIORITY[MailEventType.SHUTDOWN_REQUEST] == 0

    def test_broadcast_is_lowest_named(self) -> None:
        # BROADCAST_NOTICE has priority 7 per spec (highest named low-priority)
        assert EVENT_PRIORITY[MailEventType.BROADCAST_NOTICE] == 7

    def test_all_values_non_negative(self) -> None:
        for evt, prio in EVENT_PRIORITY.items():
            assert prio >= 0, f"{evt} has negative priority {prio}"

    def test_priority_ordering(self) -> None:
        """Key priorities follow spec ordering."""
        p = EVENT_PRIORITY
        assert p[MailEventType.SHUTDOWN_REQUEST] < p[MailEventType.APPROVAL_RESPONSE]
        assert p[MailEventType.APPROVAL_RESPONSE] < p[MailEventType.ANSWER]
        assert p[MailEventType.ANSWER] < p[MailEventType.TASK_ASSIGNMENT]
        assert p[MailEventType.TASK_ASSIGNMENT] < p[MailEventType.QUESTION]
        assert p[MailEventType.QUESTION] < p[MailEventType.PROGRESS_NOTICE]
        assert p[MailEventType.PROGRESS_NOTICE] < p[MailEventType.BROADCAST_NOTICE]
