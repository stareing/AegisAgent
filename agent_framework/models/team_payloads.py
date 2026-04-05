"""Typed payload models for team mailbox events (v4.0).

Each MailEventType has a corresponding frozen payload model.
TeamMailbox validates that the payload matches the event type on send.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskAssignmentPayload(BaseModel, frozen=True):
    """Payload for TASK_ASSIGNMENT events."""

    task_id: str
    task_description: str
    priority: int = 0
    deadline_ms: int | None = None
    required_tools: list[str] = Field(default_factory=list)


class TaskClaimRequestPayload(BaseModel, frozen=True):
    """Payload for TASK_CLAIM_REQUEST events (teammate self-selects)."""

    task_id: str
    reason: str = ""


class PlanSubmissionPayload(BaseModel, frozen=True):
    """Payload for PLAN_SUBMISSION events."""

    plan_id: str
    plan_content: str
    file_paths: list[str] = Field(default_factory=list)


class PlanApprovalPayload(BaseModel, frozen=True):
    """Payload for APPROVAL_RESPONSE events."""

    plan_id: str
    approved: bool
    feedback: str = ""


class QuestionPayload(BaseModel, frozen=True):
    """Payload for QUESTION events (teammate asks lead)."""

    question: str
    context: str = ""
    options: list[str] = Field(default_factory=list)


class AnswerPayload(BaseModel, frozen=True):
    """Payload for ANSWER events (lead answers teammate)."""

    answer: str
    original_question_id: str | None = None


class ShutdownRequestPayload(BaseModel, frozen=True):
    """Payload for SHUTDOWN_REQUEST events."""

    reason: str = ""
    graceful: bool = True
    timeout_ms: int = 30_000


class ShutdownAckPayload(BaseModel, frozen=True):
    """Payload for SHUTDOWN_ACK events."""

    accepted: bool = True
    reason: str = ""


class PermissionSyncPayload(BaseModel, frozen=True):
    """Leader broadcasts permission config to workers."""

    approval_mode: str = "DEFAULT"
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = Field(default_factory=list)


class ProgressNoticePayload(BaseModel, frozen=True):
    """Payload for PROGRESS_NOTICE events."""

    task_id: str = ""
    summary: str = ""
    progress_pct: float | None = None
    tool_count: int = 0
    token_count: int = 0


class ErrorNoticePayload(BaseModel, frozen=True):
    """Payload for ERROR_NOTICE events."""

    error_type: str = ""
    error_message: str = ""
    task_id: str = ""
    recoverable: bool = False


class BroadcastNoticePayload(BaseModel, frozen=True):
    """Payload for BROADCAST_NOTICE events."""

    message: str = ""
    importance: str = "info"  # info | warning | critical


# Mapping from MailEventType name → expected payload class.
# Used by TeamMailbox.send() for validation.
PAYLOAD_TYPE_MAP: dict[str, type[BaseModel]] = {
    "TASK_ASSIGNMENT": TaskAssignmentPayload,
    "TASK_CLAIM_REQUEST": TaskClaimRequestPayload,
    "PLAN_SUBMISSION": PlanSubmissionPayload,
    "APPROVAL_RESPONSE": PlanApprovalPayload,
    "QUESTION": QuestionPayload,
    "ANSWER": AnswerPayload,
    "SHUTDOWN_REQUEST": ShutdownRequestPayload,
    "SHUTDOWN_ACK": ShutdownAckPayload,
    "PROGRESS_NOTICE": ProgressNoticePayload,
    "ERROR_NOTICE": ErrorNoticePayload,
    "BROADCAST_NOTICE": BroadcastNoticePayload,
}
