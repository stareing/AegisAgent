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
        """Spawn a teammate as async sub-agent. Returns agent_id.

        Actually starts a real sub-agent via SubAgentRuntime.spawn_async().
        When the sub-agent completes, _on_teammate_done() sends the result
        back to Lead's mailbox as PROGRESS_NOTICE.
        """
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

        # Actually spawn sub-agent via runtime
        if self._runtime is not None:
            from agent_framework.models.subagent import SpawnMode, SubAgentSpec

            # Inject team protocol instructions into the task so the
            # spawned agent knows it's a teammate and can use team/mail tools
            team_task = (
                f"[TEAM PROTOCOL] You are a teammate in team '{self._team_id}'. "
                f"Your role is '{role}'. Your agent_id is '{agent_id}'. "
                f"The lead agent_id is '{self._lead_id}'. "
                f"You have access to team() and mail() tools. "
                f"When you finish your task, use mail(action='send', to='{self._lead_id}', "
                f"event_type='PROGRESS_NOTICE', payload={{\"status\": \"completed\", "
                f"\"summary\": \"<your result>\"}}) to report back. "
                f"If you need help, use mail(action='send', to='{self._lead_id}', "
                f"event_type='QUESTION', payload={{\"request_id\": \"q1\", "
                f"\"question\": \"<your question>\"}}).\n\n"
                f"[TASK] {task_input}"
            )

            spec = SubAgentSpec(
                parent_run_id=self._team_id,
                spawn_id=spawn_id,
                task_input=team_task,
                mode=SpawnMode.EPHEMERAL,
                skill_id=skill_id,
                max_iterations=10,
            )
            # Spawn async — returns immediately, runs in background
            actual_spawn_id = await self._runtime.spawn_async(spec, None)

            # Launch background task to collect result and report to Lead
            import asyncio

            async def _safe_watch(aid: str, sid: str, r: str, t: str) -> None:
                try:
                    await self._watch_teammate(aid, sid, r, t)
                except Exception as exc:
                    logger.error("team.watch_teammate.crashed",
                                 agent_id=aid, spawn_id=sid, error=str(exc))
                    from agent_framework.models.team import MailEvent, MailEventType
                    self._mailbox.send(MailEvent(
                        team_id=self._team_id,
                        from_agent=aid,
                        to_agent=self._lead_id,
                        event_type=MailEventType.ERROR_NOTICE,
                        payload={"error": f"Watch task crashed: {exc}", "spawn_id": sid},
                    ))
                    try:
                        from agent_framework.models.team import TeamMemberStatus
                        self._registry.update_status(aid, TeamMemberStatus.FAILED)
                    except Exception:
                        pass

            asyncio.create_task(_safe_watch(agent_id, actual_spawn_id, role, task_input))
        else:
            # No runtime — teammate cannot execute, mark as failed
            logger.error("team.spawn.no_runtime", agent_id=agent_id)
            self._registry.update_status(agent_id, TeamMemberStatus.FAILED)
            self._mailbox.send(MailEvent(
                team_id=self._team_id,
                from_agent=agent_id,
                to_agent=self._lead_id,
                event_type=MailEventType.ERROR_NOTICE,
                payload={"error": "SubAgentRuntime not configured, cannot execute"},
            ))
            return agent_id

        self._registry.update_status(agent_id, TeamMemberStatus.WORKING)
        logger.info("team.teammate_spawned", agent_id=agent_id, role=role, team_id=self._team_id)
        return agent_id

    async def _watch_teammate(
        self, agent_id: str, spawn_id: str, role: str, task: str,
    ) -> None:
        """Background task: poll for teammate completion, then report to Lead."""
        import asyncio
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        if self._runtime is None:
            return

        # Poll until result is available
        max_polls = 600  # 5 minutes at 0.5s interval
        for _ in range(max_polls):
            result = await self._runtime.collect_result(spawn_id, wait=False)
            if result is not None:
                # Report result to Lead via mailbox
                status = "completed" if result.success else "failed"
                summary = result.final_answer or result.error or ""
                self._mailbox.send(MailEvent(
                    team_id=self._team_id,
                    from_agent=agent_id,
                    to_agent=self._lead_id,
                    event_type=MailEventType.PROGRESS_NOTICE,
                    payload={
                        "status": status,
                        "summary": summary[:2000],
                        "role": role,
                        "task": task[:200],
                        "spawn_id": spawn_id,
                        "iterations_used": result.iterations_used,
                    },
                ))

                # Update member status
                new_status = TeamMemberStatus.IDLE if result.success else TeamMemberStatus.FAILED
                try:
                    self._registry.update_status(agent_id, new_status)
                except Exception:
                    pass

                logger.info(
                    "team.teammate_completed",
                    agent_id=agent_id, spawn_id=spawn_id,
                    success=result.success,
                )
                return

            await asyncio.sleep(0.5)

        # Timeout
        logger.warning("team.teammate_timeout", agent_id=agent_id, spawn_id=spawn_id)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=agent_id,
            to_agent=self._lead_id,
            event_type=MailEventType.ERROR_NOTICE,
            payload={"error": "teammate timed out", "spawn_id": spawn_id},
        ))
        try:
            self._registry.update_status(agent_id, TeamMemberStatus.FAILED)
        except Exception:
            pass

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
        """Advance shutdown: cancel runtime FIRST, then mark SHUTDOWN, then complete request.

        Order: runtime.cancel → registry.SHUTDOWN → shutdowns.complete
        This ensures "SHUTDOWN = runtime actually stopped".
        """
        from agent_framework.models.team import TeamMemberStatus
        member = self._registry.get(event.from_agent)
        runtime_cancelled = False

        # Step 1: Cancel runtime (before marking complete)
        if self._runtime is not None and member:
            try:
                import asyncio
                asyncio.ensure_future(self._runtime.cancel(member.spawn_id))
                runtime_cancelled = True
            except Exception:
                pass

        # Step 2: Update registry to SHUTDOWN
        if member and member.status != TeamMemberStatus.SHUTDOWN:
            self._registry.update_status(event.from_agent, TeamMemberStatus.SHUTDOWN)

        # Step 3: Complete shutdown request (last — confirms full shutdown)
        request_id = event.payload.get("request_id", "")
        if request_id:
            try:
                self._shutdowns.complete(request_id)
            except Exception:
                pass

        return {"type": "shutdown_ack", "from": event.from_agent, "runtime_cancelled": True}

    def _handle_error(self, event: Any) -> dict:
        from agent_framework.models.team import TeamMemberStatus
        member = self._registry.get(event.from_agent)
        if member and member.status != TeamMemberStatus.FAILED:
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
