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
        self._pending_requests: dict[str, str] = {}
        self._role_definitions: dict[str, dict] = {}

    @property
    def team_id(self) -> str:
        return self._team_id

    def register_role_definition(self, role_name: str, frontmatter: dict) -> None:
        """Register a TEAM.md role definition for tool whitelist enforcement."""
        self._role_definitions[role_name] = frontmatter

    # ── Team lifecycle ─────────────────────────────────────────

    def create_team(self, name: str = "") -> str:
        """Initialize a team. Returns team_id.

        Lead IS registered so teammates can find lead_id for reporting.
        """
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
        # Must match factory's sub_agent_id format: sub_{spawn_id}
        agent_id = f"sub_{spawn_id}"

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
            spec = SubAgentSpec(
                parent_run_id=self._team_id,
                spawn_id=spawn_id,
                task_input=task_input,
                mode=SpawnMode.EPHEMERAL,
                skill_id=skill_id,
                max_iterations=20,
                deadline_ms=0,  # No timeout for team tasks
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

                # Status stays WORKING until result is displayed to user.
                # The terminal poll loop sets IDLE after showing the notification.
                # Only set FAILED immediately (irrecoverable).
                if not result.success:
                    try:
                        self._registry.update_status(agent_id, TeamMemberStatus.FAILED)
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
        """Advance shutdown to COMPLETED."""
        request_id = event.payload.get("request_id", "")
        if request_id:
            try:
                self._shutdowns.complete(request_id)
            except Exception:
                pass  # Already completed
        from agent_framework.models.team import TeamMemberStatus
        member = self._registry.get(event.from_agent)
        if member and member.status != TeamMemberStatus.SHUTDOWN:
            self._registry.update_status(event.from_agent, TeamMemberStatus.SHUTDOWN)

        # Actually cancel the sub-agent runtime to release resources
        if self._runtime is not None and member:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._runtime.cancel(member.spawn_id))
                else:
                    loop.run_until_complete(self._runtime.cancel(member.spawn_id))
            except Exception:
                pass  # Best-effort cleanup

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

    def _resolve_member(self, identifier: str):
        """Resolve a member by agent_id, role name, or role_<name> format."""
        # Direct lookup by agent_id
        member = self._registry.get(identifier)
        if member:
            return member
        # Strip "role_" prefix if present
        clean = identifier.replace("role_", "") if identifier.startswith("role_") else identifier
        for m in self._registry.list_members():
            if m.role == clean or m.role == identifier or m.agent_id == identifier:
                return m
        return None

    def assign_task(self, task_description: str, agent_id: str) -> dict:
        """Assign a task to a teammate. Sync wrapper around _assign_task_async.

        When called from async context (tool executor), spawns real sub-agent.
        When called from sync context (tests), just sends MailEvent.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # We're in async context — schedule the spawn
            asyncio.ensure_future(self._assign_task_async(task_description, agent_id))
        except RuntimeError:
            pass  # No running loop — sync test, skip spawn

        from agent_framework.models.team import MailEvent, MailEventType
        member = self._resolve_member(agent_id)
        role = member.role if member else "teammate"

        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=member.agent_id if member else agent_id,
            event_type=MailEventType.TASK_ASSIGNMENT,
            payload={"task": task_description},
        ))

        return {
            "assigned": True,
            "agent_id": member.agent_id if member else agent_id,
            "role": role,
            "task": task_description[:100],
            "executing": self._runtime is not None,
            "note": "Task running in background. Results will be delivered automatically via notification.",
        }

    async def _assign_task_async(self, task_description: str, agent_id: str) -> None:
        """Async implementation: spawn real sub-agent to execute the task."""
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        member = self._resolve_member(agent_id)
        role = member.role if member else "teammate"
        role_def = self._role_definitions.get(role, {})
        body = ""
        # Look up TEAM.md body from discovered definitions
        for td in getattr(self, "_discovered_teams_raw", []):
            if td.get("team_id") == role:
                body = td.get("body", "")
                break

        # Update status to WORKING
        if member and member.status != TeamMemberStatus.WORKING:
            try:
                self._registry.update_status(member.agent_id, TeamMemberStatus.WORKING)
            except Exception:
                pass

        # Actually spawn a sub-agent to execute the task
        if self._runtime is not None:
            import uuid as _uuid
            from agent_framework.models.subagent import SpawnMode, SubAgentSpec

            spawn_id = _uuid.uuid4().hex[:12]
            team_task = (
                f"[TEAM PROTOCOL] You are '{role}' in team '{self._team_id}'. "
                f"Your agent_id is '{member.agent_id if member else agent_id}'. "
                f"The lead agent_id is '{self._lead_id}'. "
                f"Report results via mail(action='send', to='{self._lead_id}', "
                f"event_type='PROGRESS_NOTICE', payload={{\"status\": \"completed\", "
                f"\"summary\": \"<result>\"}}).\n\n"
            )
            if body:
                team_task += f"[ROLE INSTRUCTIONS]\n{body}\n\n"
            team_task += f"[TASK] {task_description}"

            spec = SubAgentSpec(
                parent_run_id=self._team_id,
                spawn_id=spawn_id,
                task_input=team_task,
                mode=SpawnMode.EPHEMERAL,
                tool_name_whitelist=role_def.get("allowed-tools"),
                max_iterations=20,
                deadline_ms=0,  # No timeout for team tasks
            )
            # Spawn the sub-agent (awaited — runs reliably)
            actual_sid = await self._runtime.spawn_async(spec, None)

            # Background watcher reports result to Lead inbox
            import asyncio

            async def _safe_watch(aid: str, sid: str, r: str, t: str) -> None:
                try:
                    await self._watch_teammate(aid, sid, r, t)
                except Exception as exc:
                    logger.warning("team.assign.watch_failed", error=str(exc))

            asyncio.create_task(_safe_watch(
                member.agent_id if member else agent_id,
                spawn_id, role, task_description,
            ))

            logger.info("team.assign.spawned", role=role, spawn_id=spawn_id,
                         task=task_description[:80])

        # Also send MailEvent for protocol tracking
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=member.agent_id if member else agent_id,
            event_type=MailEventType.TASK_ASSIGNMENT,
            payload={"task": task_description},
        ))

        return {
            "assigned": True,
            "agent_id": member.agent_id if member else agent_id,
            "role": role,
            "task": task_description[:100],
            "executing": self._runtime is not None,
        }

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

    def get_team_status(self, caller_id: str = "", show_self: bool = False) -> dict:
        """Return current team status with identity awareness.

        Default: hides the caller from the list (you don't need to
        see yourself). Set show_self=True to include all members.
        The caller's entry is tagged is_you=True when shown.
        """
        members = self._registry.list_members()
        member_list = []
        for m in members:
            is_self = m.agent_id == caller_id
            if is_self and not show_self:
                continue
            entry = {
                "agent_id": m.agent_id,
                "role": m.role,
                "status": m.status.value,
            }
            if is_self:
                entry["is_you"] = True
            member_list.append(entry)
        # Full list with is_you marker (always includes self)
        all_members = []
        for m in members:
            entry = {
                "agent_id": m.agent_id,
                "role": m.role,
                "status": m.status.value,
            }
            if m.agent_id == caller_id:
                entry["is_you"] = True
            all_members.append(entry)

        # Available roles from TEAM.md definitions (can be spawned)
        available_roles = []
        active_roles = {m.role for m in members if m.role != "lead"}
        for role_name, role_def in self._role_definitions.items():
            available_roles.append({
                "role": role_name,
                "description": role_def.get("description", ""),
                "spawned": role_name in active_roles,
            })

        return {
            "team_id": self._team_id,
            "lead": self._lead_id,
            "your_id": caller_id,
            "your_role": "lead" if caller_id == self._lead_id else "teammate",
            "available_roles": available_roles,
            "teammate_count": len(member_list),
            "teammates": member_list,
            "members": all_members,
            "note": (
                "You are the lead. Use team(action='spawn', role='<role>', task='<task>') to create teammates. "
                "Do NOT send mail to yourself."
                if caller_id == self._lead_id and not member_list
                else (
                    "You are the lead. Teammates listed below are your sub-agents. Do NOT send mail to yourself."
                    if caller_id == self._lead_id
                    else f"You are teammate '{caller_id}'. Others listed are your peers. The lead is '{self._lead_id}'."
                )
            ),
            "pending_plans": len(self._plans.list_pending()),
            "pending_shutdowns": len(self._shutdowns.list_pending()),
        }
