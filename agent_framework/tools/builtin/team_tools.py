"""Built-in tools for Agent Team interaction.

Two polymorphic tools covering all team scenarios:
- team(action=...) — Team management (Lead + Teammate)
- mail(action=...) — Mailbox interaction (all agents)

Design: 2 tools × ~200 tokens each = ~400 tokens total.
vs. 17 individual tools × ~200 tokens = ~3400 tokens (87% saving).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

if TYPE_CHECKING:
    from agent_framework.tools.executor import ToolExecutor

logger = get_logger(__name__)

# Permission routing — enforced at execution time, not in schema
_LEAD_ACTIONS = frozenset({
    "create", "spawn", "assign", "approve", "reject", "answer",
    "shutdown", "collect", "create_task", "cleanup",
})


@tool(
    name="team",
    description=(
        "Team management. Actions: status, assign, create_task, claim, "
        "complete_task, list_tasks, answer, approve, reject, shutdown, cleanup."
    ),
    category="team",
    require_confirm=False,
    tags=["system", "delegation", "team"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def team(
    action: str,
    task: str = "",
    target: str = "",
    message: str = "",
    depends_on: list[str] | None = None,
) -> dict:
    """Team management tool.

    Simplified interface — most context is resolved automatically:
    - action: what to do (status/assign/create_task/claim/complete_task/etc.)
    - task: task description or title (for assign/create_task/answer/complete_task)
    - target: who or what to act on (agent_id or task_id, auto-resolved)
    - message: feedback text (for reject/answer)
    - depends_on: task dependency list (for create_task only)

    Examples:
      team(action="status")
      team(action="assign", target="role_coder", task="写 hello.py")
      team(action="create_task", task="实现登录模块")
      team(action="create_task", task="编写测试", depends_on=["task_xxx"])
      team(action="claim")
      team(action="complete_task", target="task_xxx", task="已完成")
      team(action="answer", task="use pytest framework")
      team(action="cleanup")
    """
    raise RuntimeError("team tool must be routed through ToolExecutor.")


async def execute_team(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute team tool via ToolExecutor.

    Parameter resolution strategy — the LLM only provides:
      action, task, target, message, depends_on
    Everything else is resolved from executor context:
      agent_id     ← executor._current_spawn_id
      agent_role   ← executor._current_agent_role
      caller       ← executor._current_spawn_id
      request_id   ← auto-resolved from coordinator._pending_requests
    """
    coordinator = getattr(executor, "_team_coordinator", None)
    if coordinator is None:
        return {"error": "Team system not configured."}

    action = args.get("action", "")

    # ── Auto-resolve context from executor ─────────────────
    caller_id = getattr(executor, "_current_spawn_id", "")
    caller_role = getattr(executor, "_current_agent_role", "teammate")
    show_identity = getattr(executor, "_team_show_identity", False)
    identity = {"_your_id": caller_id, "_your_role": caller_role} if show_identity else {}

    # Unified "target" parameter: could be agent_id, task_id, or request_id
    # depending on the action. Also accept legacy field names.
    target = (args.get("target", "")
              or args.get("agent_id", "")
              or args.get("task_id", "")
              or args.get("request_id", ""))
    # Coerce numeric task_id from legacy callers
    if isinstance(target, int) and target > 0:
        target = str(target)

    task_text = args.get("task", "") or args.get("title", "") or args.get("answer", "")
    feedback = args.get("message", "") or args.get("feedback", "") or args.get("reason", "")

    # ── Permission check ───────────────────────────────────
    if action in _LEAD_ACTIONS and caller_role != "lead":
        return {**identity, "error": f"Permission denied: '{action}' is a lead-only action"}

    # ── Actions ────────────────────────────────────────────

    if action == "status":
        return coordinator.get_team_status(caller_id=caller_id)

    if action == "assign":
        # target = agent_id to assign to; task = task description
        result = coordinator.assign_task(task_text, target)
        return {**identity, **result}

    if action == "create_task":
        result = coordinator.create_task(
            title=task_text,
            description=args.get("description", ""),
            depends_on=args.get("depends_on"),
        )
        return {**identity, **result}

    if action == "claim":
        # target = specific task_id (optional); caller auto-resolved
        result = coordinator.claim_task(caller_id, target)
        return {**identity, **result}

    if action == "complete_task":
        # target = task_id; task = result summary
        result = coordinator.complete_task(
            target, result=task_text, agent_id=caller_id,
        )
        return {**identity, **result}

    if action == "fail_task":
        result = coordinator.fail_task(target, error=feedback)
        return {**identity, **result}

    if action == "list_tasks":
        return {**identity, **coordinator.list_tasks()}

    if action == "answer":
        # Auto-resolve request_id: if target looks like a request_id use it,
        # otherwise find the latest pending request from the target agent.
        request_id = ""
        to_agent = ""
        if target.startswith("q_") or target.startswith("req_"):
            request_id = target
        elif target:
            # target is an agent_id — find their pending request
            to_agent = target
            for rid, from_agent in coordinator._pending_requests.items():
                if from_agent == target:
                    request_id = rid
                    break
        else:
            # No target — use the most recent pending request
            if coordinator._pending_requests:
                request_id = next(iter(coordinator._pending_requests))

        answer_text = task_text or feedback
        if not request_id and not to_agent:
            return {**identity, "error": "No pending question to answer"}
        coordinator.answer_question(request_id, answer_text, to_agent=to_agent)
        return {**identity, "answered": True}

    if action == "approve":
        # Auto-resolve: target = request_id, or find latest pending plan
        request_id = target
        if not request_id:
            pending = coordinator._plans.list_pending()
            if pending:
                request_id = pending[0].request_id
        if not request_id:
            return {**identity, "error": "No pending plan to approve"}
        coordinator.approve_plan(request_id, feedback)
        return {**identity, "approved": True}

    if action == "reject":
        request_id = target
        if not request_id:
            pending = coordinator._plans.list_pending()
            if pending:
                request_id = pending[0].request_id
        if not request_id:
            return {**identity, "error": "No pending plan to reject"}
        coordinator.reject_plan(request_id, feedback)
        return {**identity, "rejected": True}

    if action == "shutdown":
        if target:
            rid = coordinator.shutdown_teammate(target, feedback)
            return {**identity, "request_id": rid, "shutdown_requested": True}
        rids = coordinator.shutdown_team()
        return {**identity, "request_ids": rids, "team_shutdown_requested": True}

    if action == "cleanup":
        return {**identity, **coordinator.cleanup_team()}

    if action == "spawn":
        role = args.get("role", "") or target
        spawned_id = await coordinator.spawn_teammate(
            role=role or "teammate", task_input=task_text,
        )
        return {**identity, "agent_id": spawned_id, "spawned": True}

    if action == "create":
        team_id = coordinator.create_team(task_text or target)
        return {**identity, "team_id": team_id, "created": True}

    if action == "collect":
        processed = coordinator.process_inbox()
        return {**identity, "events_processed": len(processed), "events": processed}

    return {**identity, "error": f"Unknown team action: {action}"}


@tool(
    name="mail",
    description=(
        "Mailbox: send/broadcast/read/reply/publish/subscribe between agents."
    ),
    category="team",
    require_confirm=False,
    tags=["system", "delegation", "team"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def mail(
    action: str,
    to: str = "",
    message: str = "",
    topic: str = "",
) -> dict:
    """Mailbox interaction tool.

    Simplified interface — most context resolved automatically:
    - action: send/broadcast/read/reply/publish/subscribe
    - to: recipient agent_id (for send/reply)
    - message: text content
    - topic: for publish/subscribe

    Examples:
      mail(action="send", to="role_coder", message="请检查代码")
      mail(action="broadcast", message="注意新需求")
      mail(action="read")
      mail(action="reply", to="role_coder", message="已收到")
      mail(action="publish", topic="findings.security", message="发现漏洞")
      mail(action="subscribe", topic="alerts.*")
    """
    raise RuntimeError("mail tool must be routed through ToolExecutor.")


async def execute_mail(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute mail tool via ToolExecutor.

    Parameter resolution — LLM only provides: action, to, message, topic.
    Everything else auto-resolved:
      from_agent  ← executor._current_spawn_id
      team_id     ← executor._current_team_id
      event_type  ← auto (BROADCAST_NOTICE for send, auto-detect for reply)
      event_id    ← auto-resolve from most recent message from 'to' agent
      payload     ← built from 'message' string
      topic_pattern ← same as 'topic' for subscribe/unsubscribe
    """
    mailbox = getattr(executor, "_team_mailbox", None)
    if mailbox is None:
        return {"error": "Team mailbox not configured"}

    action = args.get("action", "")
    agent_id = getattr(executor, "_current_spawn_id", "")
    agent_role = getattr(executor, "_current_agent_role", "teammate")
    team_id = getattr(executor, "_current_team_id", "")

    # Auto-resolve fields
    to = args.get("to", "")
    msg = args.get("message", "")
    topic = args.get("topic", "") or args.get("topic_pattern", "")
    payload = args.get("payload") or {"message": msg}
    event_type_str = args.get("event_type", "")

    # Identity injection
    show_identity = getattr(executor, "_team_show_identity", False)
    identity = {"_your_id": agent_id, "_your_role": agent_role} if show_identity else {}

    if action == "send":
        if to == agent_id:
            return {**identity, "error": f"Cannot send to yourself ('{agent_id}')."}

        from agent_framework.models.team import MailEvent, MailEventType
        # Auto event_type: default BROADCAST_NOTICE, or parse if provided
        if event_type_str:
            try:
                evt_type = MailEventType(event_type_str)
            except ValueError:
                evt_type = MailEventType.BROADCAST_NOTICE
        else:
            evt_type = MailEventType.BROADCAST_NOTICE

        event = MailEvent(
            team_id=team_id, from_agent=agent_id, to_agent=to,
            event_type=evt_type, payload=payload,
        )
        sent = mailbox.send(event)
        return {**identity, "sent": True, "event_id": sent.event_id}

    if action == "broadcast":
        from agent_framework.models.team import MailEvent, MailEventType
        event = MailEvent(
            team_id=team_id, from_agent=agent_id, to_agent="*",
            event_type=MailEventType.BROADCAST_NOTICE, payload=payload,
        )
        sent_list = mailbox.broadcast(event)
        return {**identity, "broadcast": True, "recipients": len(sent_list)}

    if action == "read":
        limit_val = args.get("limit") or None
        events = mailbox.read_inbox(agent_id, limit=limit_val)
        return {
            **identity,
            "messages": [
                {"event_id": e.event_id, "from": e.from_agent,
                 "type": e.event_type.value, "payload": e.payload}
                for e in events
            ],
            "count": len(events),
        }

    if action == "reply":
        # Auto-resolve event_id: find the most recent message from 'to' agent
        event_id = args.get("event_id", "")
        if not event_id and to:
            # Peek inbox for recent message from 'to' to reply to
            recent = mailbox.peek_inbox(agent_id)
            for e in reversed(recent):
                if e.from_agent == to:
                    event_id = e.event_id
                    break
        if not event_id:
            return {**identity, "error": "No message found to reply to"}
        reply_event = mailbox.reply(event_id, payload, source=agent_id)
        return {**identity, "replied": True, "event_id": reply_event.event_id}

    if action == "ack":
        event_id = args.get("event_id", "")
        if not event_id:
            return {**identity, "error": "event_id required"}
        mailbox.ack(agent_id, event_id)
        return {**identity, "acked": True}

    if action == "publish":
        if not topic:
            return {**identity, "error": "topic required for publish"}
        sent_list = mailbox.publish(topic, payload, source=agent_id, team_id=team_id)
        return {**identity, "published": True, "recipients": len(sent_list)}

    if action == "subscribe":
        if not topic:
            return {**identity, "error": "topic required for subscribe"}
        mailbox.subscribe(agent_id, topic)
        return {**identity, "subscribed": True, "pattern": topic}

    if action == "unsubscribe":
        if not topic:
            return {**identity, "error": "topic required for unsubscribe"}
        mailbox.unsubscribe(agent_id, topic)
        return {**identity, "unsubscribed": True, "pattern": topic}

    return {**identity, "error": f"Unknown mail action: {action}"}
