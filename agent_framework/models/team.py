"""Team data models for Agent Team collaboration protocol.

Defines structured mailbox events (MailEvent), typed payloads,
team member state machine, plan approval, and shutdown handshake models.
Event and request models are frozen (immutable) to prevent tampering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.models.subagent import AckLevel


# ---------------------------------------------------------------------------
# MailEventType — 16 structured event types
# ---------------------------------------------------------------------------

class MailEventType(str, Enum):
    """Event types exchanged through the team mailbox."""

    TASK_ASSIGNMENT = "TASK_ASSIGNMENT"
    TASK_CLAIM_REQUEST = "TASK_CLAIM_REQUEST"
    TASK_CLAIMED_NOTICE = "TASK_CLAIMED_NOTICE"
    TASK_HANDOFF_REQUEST = "TASK_HANDOFF_REQUEST"
    TASK_HANDOFF_RESPONSE = "TASK_HANDOFF_RESPONSE"
    PLAN_SUBMISSION = "PLAN_SUBMISSION"
    APPROVAL_RESPONSE = "APPROVAL_RESPONSE"
    QUESTION = "QUESTION"
    ANSWER = "ANSWER"
    PROGRESS_NOTICE = "PROGRESS_NOTICE"
    STATUS_PING = "STATUS_PING"
    STATUS_REPLY = "STATUS_REPLY"
    SHUTDOWN_REQUEST = "SHUTDOWN_REQUEST"
    SHUTDOWN_ACK = "SHUTDOWN_ACK"
    ERROR_NOTICE = "ERROR_NOTICE"
    BROADCAST_NOTICE = "BROADCAST_NOTICE"


# ---------------------------------------------------------------------------
# MailEvent — structured mailbox event (frozen)
# ---------------------------------------------------------------------------

class MailEvent(BaseModel, frozen=True):
    """Immutable structured event exchanged through the team mailbox.

    ``to_agent="*"`` denotes a broadcast event.
    ``request_id`` and ``correlation_id`` enable request/response pairing.
    """

    event_id: str = ""
    team_id: str = ""
    from_agent: str = ""
    to_agent: str = ""
    event_type: MailEventType
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str | None = None
    correlation_id: str | None = None
    requires_ack: bool = False
    ack_level: AckLevel = AckLevel.NONE
    schema_version: str = "1.1"
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# TeamMemberStatus — 8-state lifecycle
# ---------------------------------------------------------------------------

class TeamMemberStatus(str, Enum):
    """Lifecycle states for a team member.

    State machine:
        IDLE → WORKING (assign_task)
        WORKING → RESULT_READY (task completed, result stored)
        RESULT_READY → NOTIFYING (main model being notified)
        NOTIFYING → IDLE (notification delivered to main model)
        WORKING → FAILED (task error or timeout)
        RESULT_READY → FAILED (delivery failure)
        NOTIFYING → FAILED (notification delivery failure)
    """

    SPAWNING = "SPAWNING"
    WORKING = "WORKING"
    IDLE = "IDLE"
    RESULT_READY = "RESULT_READY"
    NOTIFYING = "NOTIFYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_ANSWER = "WAITING_ANSWER"
    SHUTDOWN_REQUESTED = "SHUTDOWN_REQUESTED"
    SHUTDOWN = "SHUTDOWN"
    FAILED = "FAILED"


# Terminal statuses that cannot transition out
TERMINAL_MEMBER_STATUSES: frozenset[TeamMemberStatus] = frozenset({
    TeamMemberStatus.SHUTDOWN,
    TeamMemberStatus.FAILED,
})


# ---------------------------------------------------------------------------
# TeamMember — mutable member record
# ---------------------------------------------------------------------------

class TeamMember(BaseModel):
    """A member participating in a team."""

    agent_id: str
    team_id: str = ""
    role: str = "teammate"
    status: TeamMemberStatus = TeamMemberStatus.SPAWNING
    spawn_id: str = ""
    active_task_ids: list[int] = Field(default_factory=list)
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# PlanStatus + PlanRequest
# ---------------------------------------------------------------------------

class PlanStatus(str, Enum):
    """Status of a plan approval request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


TERMINAL_PLAN_STATUSES: frozenset[PlanStatus] = frozenset({
    PlanStatus.APPROVED,
    PlanStatus.REJECTED,
    PlanStatus.CANCELLED,
})


class PlanRequest(BaseModel, frozen=True):
    """A plan submitted for approval. Immutable once created."""

    request_id: str
    team_id: str = ""
    requester: str
    approver: str
    task_id: str | None = None
    title: str = ""
    plan_text: str = ""
    risk_level: str = "low"
    status: PlanStatus = PlanStatus.PENDING
    feedback: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# ShutdownStatus + ShutdownRequest
# ---------------------------------------------------------------------------

class ShutdownStatus(str, Enum):
    """Status of a shutdown handshake."""

    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_SHUTDOWN_STATUSES: frozenset[ShutdownStatus] = frozenset({
    ShutdownStatus.COMPLETED,
    ShutdownStatus.REJECTED,
    ShutdownStatus.TIMEOUT,
    ShutdownStatus.TIMED_OUT,
})


class ShutdownRequest(BaseModel, frozen=True):
    """A shutdown handshake record. Immutable once created."""

    request_id: str
    team_id: str = ""
    requester: str
    target: str
    reason: str = ""
    status: ShutdownStatus = ShutdownStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Typed Payload models
# ---------------------------------------------------------------------------

class QuestionPayload(BaseModel, frozen=True):
    """Payload for QUESTION events."""

    request_id: str
    task_id: int | None = None
    question: str
    options: list[str] = Field(default_factory=list)
    suggested_default: str = ""


class AnswerPayload(BaseModel, frozen=True):
    """Payload for ANSWER events."""

    request_id: str
    answer: str


class PlanSubmissionPayload(BaseModel, frozen=True):
    """Payload for PLAN_SUBMISSION events."""

    request_id: str
    task_id: int | None = None
    title: str
    plan_text: str
    risk_level: str = "low"


class ApprovalPayload(BaseModel, frozen=True):
    """Payload for APPROVAL_RESPONSE events."""

    request_id: str
    approved: bool
    feedback: str = ""


class ShutdownRequestPayload(BaseModel, frozen=True):
    """Payload for SHUTDOWN_REQUEST events."""

    request_id: str
    reason: str = ""


class ShutdownAckPayload(BaseModel, frozen=True):
    """Payload for SHUTDOWN_ACK events."""

    request_id: str
    accepted: bool


# ---------------------------------------------------------------------------
# PAYLOAD_VALIDATORS — event type to payload model mapping
# ---------------------------------------------------------------------------

PAYLOAD_VALIDATORS: dict[MailEventType, type[BaseModel]] = {
    MailEventType.QUESTION: QuestionPayload,
    MailEventType.ANSWER: AnswerPayload,
    MailEventType.PLAN_SUBMISSION: PlanSubmissionPayload,
    MailEventType.APPROVAL_RESPONSE: ApprovalPayload,
    MailEventType.SHUTDOWN_REQUEST: ShutdownRequestPayload,
    MailEventType.SHUTDOWN_ACK: ShutdownAckPayload,
}


# ---------------------------------------------------------------------------
# EVENT_PRIORITY — lower number = higher priority (0 is highest)
# ---------------------------------------------------------------------------

EVENT_PRIORITY: dict[MailEventType, int] = {
    MailEventType.SHUTDOWN_REQUEST: 0,
    MailEventType.APPROVAL_RESPONSE: 1,
    MailEventType.ANSWER: 2,
    MailEventType.TASK_ASSIGNMENT: 3,
    MailEventType.TASK_HANDOFF_REQUEST: 4,
    MailEventType.QUESTION: 5,
    MailEventType.PROGRESS_NOTICE: 6,
    MailEventType.BROADCAST_NOTICE: 7,
    MailEventType.TASK_CLAIM_REQUEST: 8,
    MailEventType.TASK_CLAIMED_NOTICE: 8,
    MailEventType.TASK_HANDOFF_RESPONSE: 8,
    MailEventType.PLAN_SUBMISSION: 8,
    MailEventType.STATUS_PING: 8,
    MailEventType.STATUS_REPLY: 8,
    MailEventType.SHUTDOWN_ACK: 8,
    MailEventType.ERROR_NOTICE: 8,
}


# ---------------------------------------------------------------------------
# TeamNotification — structured notification for main model consumption
# ---------------------------------------------------------------------------

class TeamNotificationType(str, Enum):
    """Type of team notification event."""

    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    QUESTION = "QUESTION"
    PLAN_SUBMISSION = "PLAN_SUBMISSION"
    BROADCAST = "BROADCAST"
    ERROR = "ERROR"


class TeamNotification(BaseModel, frozen=True):
    """Structured notification from team member to main model.

    Replaces the loose dict that was previously appended to
    _pending_team_notifications. Immutable once created.
    """

    team_id: str
    agent_id: str
    role: str
    notification_type: TeamNotificationType
    status: str
    summary: str
    task: str = ""
    request_id: str = ""
    correlation_id: str = ""
    spawn_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
