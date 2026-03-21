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
        # Auto-run callback: called with (role, status, summary, agent_id, task, spawn_id)
        # when team results arrive. Set by AgentFramework to trigger auto-notification.
        self._on_result_callback: Any = None
        # Notification policy: determines which inbox events escalate to main model.
        self._notification_policy: Any = None
        # Escalation callback: called with (role, event_type, summary, agent_id)
        # for non-task events (QUESTION, PLAN, BROADCAST) that match the policy.
        self._on_event_escalation: Any = None
        # Active teammate conversation contexts: agent_id → context dict.
        # Tracks multi-run conversations so teammates can be resumed after Q&A.
        self._active_teammate_ctx: dict[str, dict] = {}
        # Pending answer delivery: agent_id → answer text.
        # Set by answer_question(), consumed by _watch_teammate continuation.
        self._pending_answers: dict[str, str] = {}
        # Pending approval delivery: agent_id → {"approved": bool, "feedback": str}.
        # Set by approve_plan()/reject_plan(), consumed by _watch_teammate continuation.
        self._pending_approvals: dict[str, dict] = {}

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
        """Background task: poll for teammate completion with Q&A cycle support.

        Supports multi-run conversations:
        1. Poll until sub-agent run completes
        2. Check if the sub-agent asked a question (QUESTION in lead inbox)
        3. If yes: set WAITING_ANSWER, wait for answer, then spawn continuation run
        4. If no: this is the final result → RESULT_READY → notify main model
        5. Repeat from (1) for continuation runs

        State flow:
            WORKING → WAITING_ANSWER → WORKING → ... → RESULT_READY
        """
        import asyncio
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        if self._runtime is None:
            return

        current_spawn_id = spawn_id
        conversation_history: list[str] = [f"[Original Task] {task}"]
        max_rounds = 10  # Max Q&A rounds before forcing completion

        for round_num in range(max_rounds):
            # Poll until current run completes
            result = await self._poll_for_result(current_spawn_id, timeout_polls=600)

            if result is None:
                # Timeout
                logger.warning("team.teammate_timeout", agent_id=agent_id,
                               spawn_id=current_spawn_id, round=round_num)
                self._mailbox.send(MailEvent(
                    team_id=self._team_id, from_agent=agent_id,
                    to_agent=self._lead_id,
                    event_type=MailEventType.ERROR_NOTICE,
                    payload={"error": "teammate timed out", "spawn_id": current_spawn_id},
                ))
                try:
                    self._registry.update_status(agent_id, TeamMemberStatus.FAILED)
                except Exception:
                    pass
                return

            summary = result.final_answer or result.error or ""
            conversation_history.append(f"[Round {round_num + 1} output] {summary[:2000]}")

            # Check if this run asked a question or submitted a plan
            pending_question = self._check_pending_question(agent_id)
            pending_plan = self._check_pending_plan(agent_id) if not pending_question else None

            if pending_question and result.success:
                # Sub-agent asked a question — enter WAITING_ANSWER
                request_id = pending_question.get("request_id", "")
                question_text = pending_question.get("question", "")

                logger.info("team.teammate_waiting_answer",
                            agent_id=agent_id, role=role, request_id=request_id,
                            question=question_text[:100])

                try:
                    self._registry.update_status(agent_id, TeamMemberStatus.WAITING_ANSWER)
                except Exception:
                    pass

                # Escalate QUESTION to main model
                if self._on_event_escalation is not None:
                    try:
                        await self._on_event_escalation(
                            role=role, event_type="QUESTION",
                            summary=f"Question from {role}: {question_text}",
                            agent_id=agent_id, request_id=request_id,
                        )
                    except Exception:
                        pass

                # Wait for answer (delivered via answer_question → _pending_answers)
                answer = await self._wait_for_answer(agent_id, timeout_seconds=300)

                if answer is not None:
                    conversation_history.append(f"[Answer to your question] {answer}")
                    try:
                        self._registry.update_status(agent_id, TeamMemberStatus.WORKING)
                    except Exception:
                        pass
                    continuation_spawn_id = await self._spawn_continuation(
                        agent_id=agent_id, role=role, task=task,
                        conversation_history=conversation_history, answer=answer,
                    )
                    if continuation_spawn_id:
                        current_spawn_id = continuation_spawn_id
                        continue
                    logger.warning("team.continuation_spawn_failed",
                                   agent_id=agent_id, role=role)
                else:
                    logger.warning("team.teammate_answer_timeout",
                                   agent_id=agent_id, role=role)

            elif pending_plan and result.success:
                # Sub-agent submitted a plan — enter WAITING_APPROVAL
                request_id = pending_plan.get("request_id", "")
                title = pending_plan.get("title", "")

                logger.info("team.teammate_waiting_approval",
                            agent_id=agent_id, role=role, request_id=request_id,
                            title=title[:100])

                try:
                    self._registry.update_status(agent_id, TeamMemberStatus.WAITING_APPROVAL)
                except Exception:
                    pass

                # Escalate PLAN to main model
                if self._on_event_escalation is not None:
                    try:
                        await self._on_event_escalation(
                            role=role, event_type="PLAN_SUBMISSION",
                            summary=f"Plan from {role}: {title}",
                            agent_id=agent_id, request_id=request_id,
                        )
                    except Exception:
                        pass

                # Wait for approval (delivered via approve/reject_plan → _pending_approvals)
                approval = await self._wait_for_approval(agent_id, timeout_seconds=300)

                if approval is not None:
                    approved = approval.get("approved", False)
                    feedback = approval.get("feedback", "")
                    status_word = "approved" if approved else "rejected"
                    continuation_text = f"Your plan was {status_word}."
                    if feedback:
                        continuation_text += f" Feedback: {feedback}"
                    conversation_history.append(f"[Plan {status_word}] {continuation_text}")
                    try:
                        self._registry.update_status(agent_id, TeamMemberStatus.WORKING)
                    except Exception:
                        pass
                    continuation_spawn_id = await self._spawn_continuation(
                        agent_id=agent_id, role=role, task=task,
                        conversation_history=conversation_history,
                        answer=continuation_text,
                    )
                    if continuation_spawn_id:
                        current_spawn_id = continuation_spawn_id
                        continue
                    logger.warning("team.continuation_spawn_failed",
                                   agent_id=agent_id, role=role)
                else:
                    logger.warning("team.teammate_approval_timeout",
                                   agent_id=agent_id, role=role)

            # This is the final result — no pending question or answer timeout
            await self._finalize_teammate_result(
                agent_id=agent_id, spawn_id=current_spawn_id,
                role=role, task=task, result=result,
            )
            return

        # Exhausted max Q&A rounds
        logger.warning("team.teammate_max_rounds", agent_id=agent_id, role=role,
                        rounds=max_rounds)
        try:
            self._registry.update_status(agent_id, TeamMemberStatus.FAILED)
        except Exception:
            pass

    async def _poll_for_result(self, spawn_id: str, timeout_polls: int = 600):
        """Poll SubAgentRuntime until result is available or timeout."""
        import asyncio

        for _ in range(timeout_polls):
            result = await self._runtime.collect_result(spawn_id, wait=False)
            if result is not None:
                return result
            await asyncio.sleep(0.5)
        return None

    def _check_pending_question(self, agent_id: str) -> dict | None:
        """Check if this agent has a pending QUESTION in the lead's inbox.

        Uses non-destructive peek so other teammates' messages are not lost.
        Only consumes the specific QUESTION event that matches this agent.
        """
        # First check already-processed pending_requests mapping
        for request_id, from_agent in list(self._pending_requests.items()):
            if from_agent == agent_id:
                return {"request_id": request_id, "question": f"(request_id={request_id})"}

        # Peek (non-destructive) at lead inbox for unprocessed QUESTION from this agent
        from agent_framework.models.team import MailEventType
        from agent_framework.notification.bus import BusAddress

        address = BusAddress(
            agent_id=self._lead_id,
            group=self._registry.get_team_id(),
        )
        envelopes = self._mailbox._bus.peek(address)

        for env in envelopes:
            event = self._mailbox._envelope_to_mail(env)
            if (event.event_type == MailEventType.QUESTION
                    and event.from_agent == agent_id):
                request_id = event.request_id or event.payload.get("request_id", "")
                question = event.payload.get("question", "")
                # Process only this specific event (saves mapping)
                self._handle_question(event)
                # Mark only this envelope as delivered (consume it)
                self._mailbox._bus._persistence.mark_delivered(env.envelope_id)
                return {"request_id": request_id, "question": question}

        return None

    def _check_pending_plan(self, agent_id: str) -> dict | None:
        """Check if this agent has a pending PLAN_SUBMISSION in the lead's inbox.

        Uses non-destructive peek so other teammates' messages are not lost.
        """
        # Check already-processed pending_requests for plan-related entries
        for request_id, from_agent in list(self._pending_requests.items()):
            if from_agent == agent_id and request_id.startswith("plan_"):
                return {"request_id": request_id, "title": f"(request_id={request_id})"}

        # Peek at lead inbox for PLAN_SUBMISSION from this agent
        from agent_framework.models.team import MailEventType
        from agent_framework.notification.bus import BusAddress

        address = BusAddress(
            agent_id=self._lead_id,
            group=self._registry.get_team_id(),
        )
        envelopes = self._mailbox._bus.peek(address)

        for env in envelopes:
            event = self._mailbox._envelope_to_mail(env)
            if (event.event_type == MailEventType.PLAN_SUBMISSION
                    and event.from_agent == agent_id):
                request_id = event.payload.get("request_id", "")
                title = event.payload.get("title", "")
                self._handle_plan(event)
                self._mailbox._bus._persistence.mark_delivered(env.envelope_id)
                return {"request_id": request_id, "title": title}

        return None

    async def _wait_for_approval(self, agent_id: str, timeout_seconds: int = 300) -> dict | None:
        """Wait for approval to be delivered via approve_plan/reject_plan.

        approve_plan()/reject_plan() put the result into _pending_approvals[agent_id].
        """
        import asyncio
        polls = int(timeout_seconds / 0.5)
        for _ in range(polls):
            approval = self._pending_approvals.pop(agent_id, None)
            if approval is not None:
                return approval
            await asyncio.sleep(0.5)
        return None

    async def _wait_for_answer(self, agent_id: str, timeout_seconds: int = 300) -> str | None:
        """Wait for an answer to be delivered to this teammate via answer_question().

        answer_question() puts the answer text into _pending_answers[agent_id].
        """
        import asyncio
        polls = int(timeout_seconds / 0.5)
        for _ in range(polls):
            answer = self._pending_answers.pop(agent_id, None)
            if answer is not None:
                return answer
            await asyncio.sleep(0.5)
        return None

    async def _spawn_continuation(
        self, agent_id: str, role: str, task: str,
        conversation_history: list[str], answer: str,
    ) -> str | None:
        """Spawn a continuation run for a teammate after receiving an answer.

        Returns the new spawn_id, or None if spawn failed.
        """
        if self._runtime is None:
            return None

        import uuid as _uuid
        from agent_framework.models.subagent import SpawnMode, SubAgentSpec

        # Use the member's agent_id as spawn_id for identity consistency
        new_spawn_id = agent_id
        role_def = self._role_definitions.get(role, {})
        body = ""
        for td in getattr(self, "_discovered_teams_raw", []):
            if td.get("team_id") == role:
                body = td.get("body", "")
                break

        continuation_task = (
            f"[TEAM PROTOCOL] You are '{role}' in team '{self._team_id}'. "
            f"Your agent_id is '{agent_id}'. "
            f"The lead agent_id is '{self._lead_id}'. "
            f"You asked a question earlier, and the lead has answered.\n\n"
        )
        if body:
            continuation_task += f"[ROLE INSTRUCTIONS]\n{body}\n\n"
        continuation_task += "[CONVERSATION HISTORY]\n"
        continuation_task += "\n".join(conversation_history[-6:])  # Last 6 entries
        continuation_task += (
            f"\n\n[CONTINUE] The lead answered your question: \"{answer}\"\n"
            f"Please continue working on the original task using this information. "
            f"When you are done, provide your final result."
        )

        spec = SubAgentSpec(
            parent_run_id=self._team_id,
            spawn_id=new_spawn_id,
            task_input=continuation_task,
            mode=SpawnMode.EPHEMERAL,
            tool_name_whitelist=role_def.get("allowed-tools"),
            max_iterations=20,
            deadline_ms=0,
        )

        try:
            actual_sid = await self._runtime.spawn_async(spec, None)
            logger.info("team.continuation_spawned", agent_id=agent_id,
                         role=role, new_spawn_id=new_spawn_id,
                         actual_sid=actual_sid)
            return actual_sid
        except Exception as exc:
            logger.error("team.continuation_spawn_error", error=str(exc))
            return None

    async def _finalize_teammate_result(
        self, agent_id: str, spawn_id: str, role: str, task: str, result: Any,
    ) -> None:
        """Finalize a teammate's result: send notification, update status, trigger callback."""
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        status = "completed" if result.success else "failed"
        summary = result.final_answer or result.error or ""

        # Send mailbox event for protocol tracking
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

        # Move to RESULT_READY (not IDLE — wait for main model to consume)
        new_status = TeamMemberStatus.RESULT_READY if result.success else TeamMemberStatus.FAILED
        try:
            self._registry.update_status(agent_id, new_status)
        except Exception:
            pass

        logger.info("team.teammate_completed", agent_id=agent_id,
                     spawn_id=spawn_id, success=result.success,
                     status=new_status.value)

        # Trigger framework-level notification callback
        if self._on_result_callback is not None:
            try:
                await self._on_result_callback(
                    role=role, status=status, summary=summary[:2000],
                    agent_id=agent_id, task=task[:200], spawn_id=spawn_id,
                )
            except Exception as cb_err:
                logger.warning("team.result_callback_failed", error=str(cb_err))

        # Clean up conversation context
        self._active_teammate_ctx.pop(agent_id, None)

    # ── Inbox processing ───────────────────────────────────────

    def process_inbox(self) -> list[dict]:
        """Read and process Lead's inbox by event priority.

        Events matching the notification policy are escalated to the main model
        via _on_event_escalation callback (QUESTION, PLAN, BROADCAST, etc.).
        """
        from agent_framework.models.team import EVENT_PRIORITY

        events = self._mailbox.read_inbox(self._lead_id)
        events.sort(key=lambda e: EVENT_PRIORITY.get(e.event_type, 8))

        processed = []
        for event in events:
            result = self._handle_event(event)
            processed.append(result)
            self._mailbox.ack(self._lead_id, event.event_id)

            # Escalate matching events to main model via policy
            self._maybe_escalate_event(event, result)

        return processed

    def _maybe_escalate_event(self, event: Any, result: dict) -> None:
        """Escalate inbox event to main model if it matches notification policy."""
        if self._on_event_escalation is None:
            return

        from agent_framework.models.team import MailEventType

        policy = self._notification_policy
        event_type = event.event_type

        # Check policy if available; otherwise escalate QUESTION and PLAN always
        should_escalate = False
        if policy is not None:
            topic = event.payload.get("topic", "")
            should_escalate = policy.should_escalate_mail_event(event_type, topic=topic)
        else:
            # Default: escalate QUESTION and PLAN_SUBMISSION
            should_escalate = event_type in (
                MailEventType.QUESTION,
                MailEventType.PLAN_SUBMISSION,
            )

        if not should_escalate:
            return

        # Build escalation summary from the processed result
        role = "unknown"
        member = self._resolve_member(event.from_agent)
        if member:
            role = member.role

        summary_parts = []
        if event_type == MailEventType.QUESTION:
            summary_parts.append(f"Question: {result.get('question', '')}")
        elif event_type == MailEventType.PLAN_SUBMISSION:
            summary_parts.append(f"Plan: {result.get('title', '')}")
        elif event_type == MailEventType.BROADCAST_NOTICE:
            summary_parts.append(f"Broadcast: {event.payload.get('message', '')}")
        else:
            summary_parts.append(f"Event: {event_type.value}")

        import asyncio
        try:
            coro = self._on_event_escalation(
                role=role,
                event_type=event_type.value,
                summary="; ".join(summary_parts),
                agent_id=event.from_agent,
                request_id=result.get("request_id", ""),
            )
            loop = asyncio.get_running_loop()
            if loop.is_running():
                asyncio.ensure_future(coro)
            else:
                loop.run_until_complete(coro)
        except RuntimeError:
            pass  # No event loop
        except Exception as exc:
            logger.warning("team.escalation_failed", error=str(exc))

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
        """Record question for Lead to answer. Saves request_id → from_agent mapping."""
        request_id = event.request_id or event.payload.get("request_id", "")
        if request_id:
            self._pending_requests[request_id] = event.from_agent
        return {
            "type": "question",
            "from": event.from_agent,
            "request_id": request_id,
            "question": event.payload.get("question", ""),
        }

    def _handle_plan(self, event: Any) -> dict:
        """Record plan submission for Lead to review. Saves request_id → from_agent mapping."""
        request_id = event.payload.get("request_id", "")
        if request_id:
            self._pending_requests[request_id] = event.from_agent
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

    def _validate_assignment_target(self, agent_id: str):
        """Return the resolved member if it can accept a new task."""
        from agent_framework.models.team import BUSY_MEMBER_STATUSES

        member = self._resolve_member(agent_id)
        if member is None:
            return None, f"Unknown teammate: {agent_id}"
        if member.status in BUSY_MEMBER_STATUSES:
            return None, (
                f"Teammate '{member.agent_id}' is busy "
                f"(status={member.status.value}) and cannot accept a new task yet."
            )
        return member, ""

    def assign_task(self, task_description: str, agent_id: str) -> dict:
        """Assign a task to a teammate. Sync wrapper around _assign_task_async.

        Atomically claims the member (IDLE → WORKING) before scheduling
        async spawn, preventing race conditions from concurrent assigns.
        """
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        member, error = self._validate_assignment_target(agent_id)
        if member is None:
            return {
                "assigned": False,
                "agent_id": agent_id,
                "task": task_description[:100],
                "error": error,
            }

        role = member.role

        # Atomically claim: IDLE → WORKING *before* scheduling async work.
        # This prevents a second assign_task() in the same tick from seeing IDLE.
        try:
            self._registry.update_status(member.agent_id, TeamMemberStatus.WORKING)
        except Exception:
            pass

        # Send TASK_ASSIGNMENT mail (single point — not duplicated in async path)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=member.agent_id,
            event_type=MailEventType.TASK_ASSIGNMENT,
            payload={"task": task_description},
        ))

        import asyncio
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(self._assign_task_async(task_description, member.agent_id))
        except RuntimeError:
            pass  # No running loop — sync test, skip spawn

        return {
            "assigned": True,
            "agent_id": member.agent_id,
            "role": role,
            "task": task_description[:100],
            "executing": self._runtime is not None,
            "note": "Task running in background. Results will be delivered automatically via notification.",
        }

    async def _assign_task_async(self, task_description: str, agent_id: str) -> None:
        """Async implementation: spawn real sub-agent to execute the task.

        Called after assign_task() has already claimed the member (IDLE → WORKING)
        and sent the TASK_ASSIGNMENT mail. This method only spawns the sub-agent.
        """
        from agent_framework.models.team import MailEvent, MailEventType, TeamMemberStatus

        member = self._resolve_member(agent_id)
        if member is None:
            logger.warning("team.assign_async.member_not_found", agent_id=agent_id)
            return
        role = member.role
        role_def = self._role_definitions.get(role, {})
        body = ""
        for td in getattr(self, "_discovered_teams_raw", []):
            if td.get("team_id") == role:
                body = td.get("body", "")
                break

        # Spawn the sub-agent (member already claimed as WORKING by assign_task)
        if self._runtime is not None:
            import uuid as _uuid
            from agent_framework.models.subagent import SpawnMode, SubAgentSpec

            my_id = member.agent_id if member else agent_id
            # Use member's agent_id as spawn_id so the factory sets
            # _current_spawn_id = my_id — ensuring mail from_agent matches
            # the team registry identity (not a random sub_xxx id).
            spawn_id = my_id
            team_task = (
                f"[TEAM PROTOCOL] You are '{role}' in team '{self._team_id}'. "
                f"Your agent_id is '{my_id}'. "
                f"The lead agent_id is '{self._lead_id}'.\n"
                f"- If you need clarification, use: mail(action='send', to='{self._lead_id}', "
                f"event_type='QUESTION', payload={{\"question\": \"<your question>\", "
                f"\"request_id\": \"q_<unique_id>\"}}). Then STOP and wait — "
                f"the lead will answer, and you will be resumed with the answer.\n"
                f"- When your task is fully complete, just provide your final answer directly.\n\n"
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
            try:
                # Spawn the sub-agent (awaited — runs reliably)
                actual_sid = await self._runtime.spawn_async(spec, None)
            except Exception as exc:
                error_msg = str(exc)
                logger.error(
                    "team.assign.spawn_failed",
                    agent_id=member.agent_id,
                    role=role,
                    error=error_msg,
                )
                # Quota exceeded or transient errors → reset to IDLE so member
                # can be retried. Only mark FAILED for permanent errors.
                is_quota_error = "quota exceeded" in error_msg.lower()
                recovery_status = (
                    TeamMemberStatus.IDLE if is_quota_error
                    else TeamMemberStatus.FAILED
                )
                try:
                    self._registry.update_status(member.agent_id, recovery_status)
                except Exception:
                    pass
                self._mailbox.send(MailEvent(
                    team_id=self._team_id,
                    from_agent=member.agent_id,
                    to_agent=self._lead_id,
                    event_type=MailEventType.ERROR_NOTICE,
                    payload={"error": f"Failed to start teammate task: {exc}"},
                ))
                return

            # Background watcher reports result to Lead inbox
            import asyncio

            async def _safe_watch(aid: str, sid: str, r: str, t: str) -> None:
                try:
                    await self._watch_teammate(aid, sid, r, t)
                except Exception as exc:
                    logger.warning("team.assign.watch_failed", error=str(exc))

            asyncio.create_task(_safe_watch(
                member.agent_id if member else agent_id,
                actual_sid, role, task_description,
            ))

            logger.info("team.assign.spawned", role=role,
                         spawn_id=spawn_id, actual_sid=actual_sid,
                         task=task_description[:80])

        # NOTE: TASK_ASSIGNMENT MailEvent is already sent by the sync wrapper
        # assign_task() — do not send a second one here.

        return {
            "assigned": True,
            "agent_id": member.agent_id if member else agent_id,
            "role": role,
            "task": task_description[:100],
            "executing": self._runtime is not None,
        }

    def approve_plan(self, request_id: str, feedback: str = "") -> None:
        """Approve a teammate's plan and trigger continuation."""
        from agent_framework.models.team import MailEvent, MailEventType
        plan = self._plans.approve(request_id, feedback)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=plan.requester,
            event_type=MailEventType.APPROVAL_RESPONSE,
            payload={"request_id": request_id, "approved": True, "feedback": feedback},
        ))
        # Deliver approval to the watcher for continuation
        self._pending_approvals[plan.requester] = {"approved": True, "feedback": feedback}
        logger.info("team.plan_approved", target=plan.requester, request_id=request_id)

    def reject_plan(self, request_id: str, feedback: str = "") -> None:
        """Reject a teammate's plan and trigger continuation."""
        from agent_framework.models.team import MailEvent, MailEventType
        plan = self._plans.reject(request_id, feedback)
        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=plan.requester,
            event_type=MailEventType.APPROVAL_RESPONSE,
            payload={"request_id": request_id, "approved": False, "feedback": feedback},
        ))
        # Deliver rejection to the watcher for continuation
        self._pending_approvals[plan.requester] = {"approved": False, "feedback": feedback}
        logger.info("team.plan_rejected", target=plan.requester, request_id=request_id)

    def answer_question(self, request_id: str, answer: str, to_agent: str = "") -> None:
        """Answer a teammate's question and trigger continuation.

        Resolves target by request_id first (from _pending_requests),
        falls back to explicit to_agent for backward compatibility.

        Also delivers the answer to _pending_answers so the background
        watcher (_watch_teammate) can resume the teammate's execution.
        """
        from agent_framework.models.team import MailEvent, MailEventType

        # Resolve target: request_id mapping takes priority
        target = self._pending_requests.pop(request_id, "") or to_agent
        if not target:
            logger.warning("team.answer.no_target", request_id=request_id)
            return

        self._mailbox.send(MailEvent(
            team_id=self._team_id,
            from_agent=self._lead_id,
            to_agent=target,
            event_type=MailEventType.ANSWER,
            payload={"request_id": request_id, "answer": answer},
        ))

        # Deliver answer to the watcher so it can spawn a continuation run
        self._pending_answers[target] = answer
        logger.info("team.answer_delivered", target=target, request_id=request_id)

    # ── Result lifecycle management ──────────────────────────────

    def mark_result_notifying(self, agent_id: str) -> None:
        """Transition RESULT_READY → NOTIFYING (main model is being notified)."""
        from agent_framework.models.team import TeamMemberStatus
        member = self._resolve_member(agent_id)
        if member and member.status == TeamMemberStatus.RESULT_READY:
            self._registry.update_status(member.agent_id, TeamMemberStatus.NOTIFYING)

    def mark_result_delivered(self, agent_id: str) -> None:
        """Transition NOTIFYING → IDLE (main model consumed the result)."""
        from agent_framework.models.team import TeamMemberStatus
        member = self._resolve_member(agent_id)
        if member and member.status in (
            TeamMemberStatus.NOTIFYING, TeamMemberStatus.RESULT_READY,
        ):
            self._registry.update_status(member.agent_id, TeamMemberStatus.IDLE)

    def mark_result_delivery_failed(self, agent_id: str, reason: str = "") -> None:
        """Transition NOTIFYING/RESULT_READY → FAILED."""
        from agent_framework.models.team import TeamMemberStatus
        member = self._resolve_member(agent_id)
        if member and member.status in (
            TeamMemberStatus.NOTIFYING, TeamMemberStatus.RESULT_READY,
        ):
            self._registry.update_status(member.agent_id, TeamMemberStatus.FAILED)
            logger.warning("team.result_delivery_failed", agent_id=agent_id, reason=reason)

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
