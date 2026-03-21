"""TeammateRuntime — Worker agent execution loop for Agent Teams.

Handles the work/idle cycle: execute tasks, report progress, ask questions,
submit plans for approval, and respond to shutdown requests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger

if TYPE_CHECKING:
    from agent_framework.team.mailbox import TeamMailbox
    from agent_framework.team.plan_registry import PlanRegistry
    from agent_framework.team.registry import TeamRegistry

logger = get_logger(__name__)


class TeammateRuntime:
    """Teammate Agent work/idle loop.

    Work phase:
    - Execute assigned task
    - Send PROGRESS_NOTICE
    - Send QUESTION (needs clarification)
    - Send PLAN_SUBMISSION (high-risk operation)
    - Complete → enter IDLE

    Idle phase:
    - Poll inbox
    - Respond to STATUS_PING
    - Respond to SHUTDOWN_REQUEST
    - Auto-claim available tasks
    """

    def __init__(
        self,
        agent_id: str,
        team_id: str,
        mailbox: TeamMailbox,
        team_registry: TeamRegistry,
        plan_registry: PlanRegistry,
    ) -> None:
        self._agent_id = agent_id
        self._team_id = team_id
        self._mailbox = mailbox
        self._registry = team_registry
        self._plans = plan_registry

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def _lead_id(self) -> str:
        """Resolve Lead's actual agent_id from registry (not hardcoded)."""
        members = self._registry.list_members()
        for m in members:
            if m.role == "lead":
                return m.agent_id
        return "lead"  # Fallback only if registry has no lead

    # ── Work phase ─────────────────────────────────────────────

    def report_progress(self, percent: int, summary: str, task_id: int | None = None) -> None:
        """Send progress update to Lead."""
        from agent_framework.models.team import MailEvent, MailEventType
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=self._lead_id,
            event_type=MailEventType.PROGRESS_NOTICE,
            payload={"percent": percent, "summary": summary, "task_id": task_id},
        ))

    def ask_question(
        self, question: str, options: list[str] | None = None,
        task_id: int | None = None,
    ) -> str:
        """Ask Lead a question. Returns request_id for matching the answer."""
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        request_id = f"q_{uuid.uuid4().hex[:12]}"
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=self._lead_id,
            event_type=MailEventType.QUESTION,
            requires_ack=True,
            request_id=request_id,
            payload={
                "request_id": request_id,
                "question": question,
                "options": options or [],
                "task_id": task_id,
            },
        ))
        self._registry.update_status(self._agent_id, TeamMemberStatus.WAITING_ANSWER)
        return request_id

    def submit_plan(
        self, title: str, plan_text: str, risk_level: str = "low",
        task_id: int | None = None,
    ) -> str:
        """Submit a plan for Lead approval. Returns request_id."""
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        plan = self._plans.create(
            requester=self._agent_id,
            approver=self._lead_id,
            plan_text=plan_text,
            title=title,
            risk_level=risk_level,
            task_id=task_id,
            team_id=self._team_id,
        )
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=self._lead_id,
            event_type=MailEventType.PLAN_SUBMISSION,
            requires_ack=True,
            request_id=plan.request_id,
            payload={
                "request_id": plan.request_id,
                "title": title,
                "plan_text": plan_text,
                "risk_level": risk_level,
                "task_id": task_id,
            },
        ))
        self._registry.update_status(self._agent_id, TeamMemberStatus.WAITING_APPROVAL)
        return plan.request_id

    def send_to_sibling(self, to_agent_id: str, message: str, payload: dict | None = None) -> None:
        """Send a direct message to a sibling teammate."""
        from agent_framework.models.team import MailEvent, MailEventType
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=to_agent_id,
            event_type=MailEventType.BROADCAST_NOTICE,
            payload={"message": message, **(payload or {})},
        ))

    # ── Idle phase ─────────────────────────────────────────────

    def read_inbox(self, limit: int | None = None) -> list[Any]:
        """Read pending messages."""
        return self._mailbox.read_inbox(self._agent_id, limit=limit)

    def handle_event(self, event: Any) -> dict:
        """Dispatch event to handler. Returns result dict."""
        from agent_framework.models.team import MailEventType

        handlers = {
            MailEventType.TASK_ASSIGNMENT: self._handle_assignment,
            MailEventType.ANSWER: self._handle_answer,
            MailEventType.APPROVAL_RESPONSE: self._handle_approval,
            MailEventType.SHUTDOWN_REQUEST: self._handle_shutdown,
            MailEventType.STATUS_PING: self._handle_ping,
            MailEventType.TASK_HANDOFF_REQUEST: self._handle_handoff,
        }
        handler = handlers.get(event.event_type, self._handle_default)
        return handler(event)

    def _handle_assignment(self, event: Any) -> dict:
        from agent_framework.models.team import TeamMemberStatus
        self._registry.update_status(self._agent_id, TeamMemberStatus.WORKING)
        return {"type": "task_assignment", "task": event.payload.get("task", "")}

    def _handle_answer(self, event: Any) -> dict:
        from agent_framework.models.team import TeamMemberStatus
        self._registry.update_status(self._agent_id, TeamMemberStatus.WORKING)
        return {"type": "answer", "request_id": event.request_id, "answer": event.payload.get("answer", "")}

    def _handle_approval(self, event: Any) -> dict:
        from agent_framework.models.team import TeamMemberStatus
        approved = event.payload.get("approved", False)
        self._registry.update_status(self._agent_id, TeamMemberStatus.WORKING)
        return {
            "type": "approval",
            "approved": approved,
            "feedback": event.payload.get("feedback", ""),
            "request_id": event.payload.get("request_id", ""),
        }

    def _handle_shutdown(self, event: Any) -> dict:
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus
        request_id = event.payload.get("request_id", "")
        self._registry.update_status(self._agent_id, TeamMemberStatus.SHUTDOWN)
        # Send ACK
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=event.from_agent,
            event_type=MailEventType.SHUTDOWN_ACK,
            payload={"request_id": request_id, "accepted": True},
        ))
        return {"type": "shutdown", "request_id": request_id}

    def _handle_ping(self, event: Any) -> dict:
        from agent_framework.models.team import MailEvent, MailEventType
        member = self._registry.get(self._agent_id)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._agent_id,
            to_agent=event.from_agent,
            event_type=MailEventType.STATUS_REPLY,
            payload={"status": member.status.value if member else "unknown"},
        ))
        return {"type": "status_reply"}

    def _handle_handoff(self, event: Any) -> dict:
        return {"type": "handoff_request", "from": event.from_agent, "payload": event.payload}

    def _handle_default(self, event: Any) -> dict:
        return {"type": event.event_type.value, "from": event.from_agent}
