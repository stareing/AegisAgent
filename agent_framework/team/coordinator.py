"""TeamCoordinator — Lead Agent orchestration loop for Agent Teams.

The Lead agent's coordination logic: spawn teammates, assign tasks,
process inbox events (questions, plans, progress), and collect results.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger

if TYPE_CHECKING:
    from agent_framework.subagent.runtime import SubAgentRuntime
    from agent_framework.team.mailbox import TeamMailbox
    from agent_framework.team.plan_registry import PlanRegistry
    from agent_framework.team.registry import TeamRegistry
    from agent_framework.team.shutdown_registry import ShutdownRegistry

logger = get_logger(__name__)


class TeamCoordinator:
    """Lead Agent orchestration loop.

    Lead each iteration:
    1. Read own inbox
    2. Process events by priority
    3. Write formal state FIRST, then send events
    4. Assign/reclaim tasks
    5. Handle QUESTION / PLAN_SUBMISSION
    6. Collect results
    """

    def __init__(
        self,
        team_id: str,
        lead_agent_id: str,
        mailbox: TeamMailbox,
        team_registry: TeamRegistry,
        plan_registry: PlanRegistry,
        shutdown_registry: ShutdownRegistry,
        sub_agent_runtime: SubAgentRuntime | None = None,
    ) -> None:
        self._team_id = team_id
        self._lead_id = lead_agent_id
        self._mailbox = mailbox
        self._registry = team_registry
        self._plans = plan_registry
        self._shutdowns = shutdown_registry
        self._runtime = sub_agent_runtime

    @property
    def team_id(self) -> str:
        return self._team_id

    # ── Team lifecycle ─────────────────────────────────────────

    def create_team(self, name: str = "") -> str:
        """Initialize a team. Returns team_id."""
        from agent_framework.models.team import TeamMember, TeamMemberStatus
        lead = TeamMember(
            agent_id=self._lead_id,
            team_id=self._team_id,
            role="lead",
            status=TeamMemberStatus.WORKING,
            joined_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._registry.register(lead)
        logger.info("team.created", team_id=self._team_id, lead=self._lead_id, name=name)
        return self._team_id

    async def spawn_teammate(
        self,
        role: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> str:
        """Spawn a teammate as LONG_LIVED sub-agent. Returns agent_id."""
        from agent_framework.models.team import (MailEvent, MailEventType,
                                                  TeamMember, TeamMemberStatus)

        spawn_id = uuid.uuid4().hex[:12]
        agent_id = f"tm_{spawn_id}"

        # Write state first
        member = TeamMember(
            agent_id=agent_id,
            team_id=self._team_id,
            role=role,
            status=TeamMemberStatus.SPAWNING,
            spawn_id=spawn_id,
            joined_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._registry.register(member)

        # Then send event
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=agent_id,
            event_type=MailEventType.TASK_ASSIGNMENT,
            payload={"task": task_input, "role": role, "skill_id": skill_id or ""},
        ))

        self._registry.update_status(agent_id, TeamMemberStatus.WORKING)
        logger.info("team.teammate_spawned", agent_id=agent_id, role=role, team_id=self._team_id)
        return agent_id

    # ── Inbox processing ───────────────────────────────────────

    def process_inbox(self) -> list[dict]:
        """Read and process Lead's inbox by event priority."""
        from agent_framework.models.team import EVENT_PRIORITY

        events = self._mailbox.read_inbox(self._lead_id)
        events.sort(key=lambda e: EVENT_PRIORITY.get(e.event_type, 8))

        processed = []
        for event in events:
            result = self._handle_event(event)
            processed.append(result)
            self._mailbox.ack(self._lead_id, event.event_id)
        return processed

    def _handle_event(self, event: Any) -> dict:
        """Route event to handler. Returns processing result."""
        from agent_framework.models.team import MailEventType

        handlers = {
            MailEventType.QUESTION: self._handle_question,
            MailEventType.PLAN_SUBMISSION: self._handle_plan,
            MailEventType.PROGRESS_NOTICE: self._handle_progress,
            MailEventType.SHUTDOWN_ACK: self._handle_shutdown_ack,
            MailEventType.ERROR_NOTICE: self._handle_error,
        }
        handler = handlers.get(event.event_type, self._handle_default)
        return handler(event)

    def _handle_question(self, event: Any) -> dict:
        """Record question for Lead to answer."""
        return {
            "type": "question",
            "from": event.from_agent,
            "request_id": event.request_id,
            "question": event.payload.get("question", ""),
        }

    def _handle_plan(self, event: Any) -> dict:
        """Record plan submission for Lead to review."""
        request_id = event.payload.get("request_id", "")
        return {
            "type": "plan_submission",
            "from": event.from_agent,
            "request_id": request_id,
            "title": event.payload.get("title", ""),
            "risk_level": event.payload.get("risk_level", "low"),
        }

    def _handle_progress(self, event: Any) -> dict:
        return {"type": "progress", "from": event.from_agent, "payload": event.payload}

    def _handle_shutdown_ack(self, event: Any) -> dict:
        """Advance shutdown to COMPLETED."""
        request_id = event.payload.get("request_id", "")
        if request_id:
            self._shutdowns.complete(request_id)
        from agent_framework.models.team import TeamMemberStatus
        self._registry.update_status(event.from_agent, TeamMemberStatus.SHUTDOWN)
        return {"type": "shutdown_ack", "from": event.from_agent}

    def _handle_error(self, event: Any) -> dict:
        from agent_framework.models.team import TeamMemberStatus
        self._registry.update_status(event.from_agent, TeamMemberStatus.FAILED)
        return {"type": "error", "from": event.from_agent, "error": event.payload.get("error", "")}

    def _handle_default(self, event: Any) -> dict:
        return {"type": event.event_type.value, "from": event.from_agent}

    # ── Task management ───────────────────────────────────────

    def assign_task(self, task_description: str, agent_id: str) -> None:
        """Assign a task to a teammate."""
        from agent_framework.models.team import MailEvent, MailEventType
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=agent_id,
            event_type=MailEventType.TASK_ASSIGNMENT,
            payload={"task": task_description},
        ))

    def approve_plan(self, request_id: str, feedback: str = "") -> None:
        """Approve a teammate's plan."""
        from agent_framework.models.team import MailEvent, MailEventType
        plan = self._plans.approve(request_id, feedback)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=plan.requester,
            event_type=MailEventType.APPROVAL_RESPONSE,
            payload={"request_id": request_id, "approved": True, "feedback": feedback},
        ))

    def reject_plan(self, request_id: str, feedback: str = "") -> None:
        """Reject a teammate's plan."""
        from agent_framework.models.team import MailEvent, MailEventType
        plan = self._plans.reject(request_id, feedback)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=plan.requester,
            event_type=MailEventType.APPROVAL_RESPONSE,
            payload={"request_id": request_id, "approved": False, "feedback": feedback},
        ))

    def answer_question(self, request_id: str, answer: str, to_agent: str) -> None:
        """Answer a teammate's question."""
        from agent_framework.models.team import MailEvent, MailEventType
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=to_agent,
            event_type=MailEventType.ANSWER,
            payload={"request_id": request_id, "answer": answer},
        ))

    # ── Shutdown ───────────────────────────────────────────────

    def shutdown_teammate(self, agent_id: str, reason: str = "") -> str:
        """Request teammate shutdown. Returns request_id."""
        from agent_framework.models.team import (MailEvent, MailEventType,
                                                  TeamMemberStatus)
        req = self._shutdowns.create(self._lead_id, agent_id, reason, self._team_id)
        self._registry.update_status(agent_id, TeamMemberStatus.SHUTDOWN_REQUESTED)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=agent_id,
            event_type=MailEventType.SHUTDOWN_REQUEST,
            payload={"request_id": req.request_id, "reason": reason},
        ))
        return req.request_id

    def shutdown_team(self) -> list[str]:
        """Request shutdown for all active teammates. Returns request_ids."""
        from agent_framework.models.team import TeamMemberStatus
        active = self._registry.list_members()
        request_ids = []
        for member in active:
            if member.agent_id == self._lead_id:
                continue
            if member.status in (TeamMemberStatus.SHUTDOWN, TeamMemberStatus.FAILED):
                continue
            rid = self.shutdown_teammate(member.agent_id, "team shutdown")
            request_ids.append(rid)
        return request_ids

    # ── Status ─────────────────────────────────────────────────

    def get_team_status(self) -> dict:
        """Return current team status summary."""
        members = self._registry.list_members()
        return {
            "team_id": self._team_id,
            "lead": self._lead_id,
            "member_count": len(members),
            "members": [
                {
                    "agent_id": m.agent_id,
                    "role": m.role,
                    "status": m.status.value,
                }
                for m in members
            ],
            "pending_plans": len(self._plans.list_pending()),
            "pending_shutdowns": len(self._shutdowns.list_pending()),
        }
