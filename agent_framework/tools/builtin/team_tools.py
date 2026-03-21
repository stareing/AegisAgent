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
    "create", "spawn", "assign", "approve", "reject", "answer", "shutdown", "collect",
})


@tool(
    name="team",
    description=(
        "Team management: create/spawn/assign/approve/reject/answer/shutdown/status/collect. "
        "Lead-only actions: create, spawn, assign, approve, reject, answer, shutdown, collect. "
        "All agents: status."
    ),
    category="team",
    require_confirm=False,
    tags=["team"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def team(
    action: str,
    name: str = "",
    role: str = "",
    task: str = "",
    task_id: int = 0,
    agent_id: str = "",
    request_id: str = "",
    answer: str = "",
    feedback: str = "",
    reason: str = "",
    skill_id: str = "",
) -> dict:
    """Team management tool."""
    raise RuntimeError("team tool must be routed through ToolExecutor.")


async def execute_team(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute team tool via ToolExecutor."""
    coordinator = getattr(executor, "_team_coordinator", None)
    if coordinator is None:
        return {"error": "Team system not configured. Use team(action='create') first."}

    action = args.get("action", "")

    # Permission check
    agent_role = getattr(executor, "_current_agent_role", "teammate")
    if action in _LEAD_ACTIONS and agent_role != "lead":
        return {"error": f"Permission denied: '{action}' is a lead-only action"}

    if action == "create":
        team_id = coordinator.create_team(args.get("name", ""))
        return {"team_id": team_id, "created": True}

    if action == "spawn":
        agent_id = await coordinator.spawn_teammate(
            role=args.get("role", "teammate"),
            task_input=args.get("task", ""),
            skill_id=args.get("skill_id") or None,
        )
        return {"agent_id": agent_id, "spawned": True}

    if action == "assign":
        coordinator.assign_task(args.get("task", ""), args.get("agent_id", ""))
        return {"assigned": True}

    if action == "approve":
        coordinator.approve_plan(args.get("request_id", ""), args.get("feedback", ""))
        return {"approved": True}

    if action == "reject":
        coordinator.reject_plan(args.get("request_id", ""), args.get("feedback", ""))
        return {"rejected": True}

    if action == "answer":
        coordinator.answer_question(
            args.get("request_id", ""), args.get("answer", ""),
            to_agent=args.get("agent_id", ""),
        )
        return {"answered": True}

    if action == "shutdown":
        agent_id = args.get("agent_id", "")
        if agent_id:
            rid = coordinator.shutdown_teammate(agent_id, args.get("reason", ""))
            return {"request_id": rid, "shutdown_requested": True}
        rids = coordinator.shutdown_team()
        return {"request_ids": rids, "team_shutdown_requested": True}

    if action == "status":
        return coordinator.get_team_status()

    if action == "collect":
        processed = coordinator.process_inbox()
        return {"events_processed": len(processed), "events": processed}

    return {"error": f"Unknown team action: {action}"}


@tool(
    name="mail",
    description=(
        "Mailbox: send/broadcast/read/ack/reply/publish/subscribe/unsubscribe events between agents. "
        "All team members can use all mail actions."
    ),
    category="team",
    require_confirm=False,
    tags=["team"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def mail(
    action: str,
    to: str = "",
    event_type: str = "",
    event_id: str = "",
    topic: str = "",
    topic_pattern: str = "",
    payload: dict | None = None,
    limit: int = 0,
    message: str = "",
) -> dict:
    """Mailbox interaction tool."""
    raise RuntimeError("mail tool must be routed through ToolExecutor.")


async def execute_mail(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute mail tool via ToolExecutor."""
    mailbox = getattr(executor, "_team_mailbox", None)
    if mailbox is None:
        return {"error": "Team mailbox not configured"}

    action = args.get("action", "")
    agent_id = getattr(executor, "_current_spawn_id", "")
    team_id = getattr(executor, "_current_team_id", "")

    if action == "send":
        from agent_framework.models.team import MailEvent, MailEventType
        event_type_str = args.get("event_type", "BROADCAST_NOTICE")
        try:
            evt_type = MailEventType(event_type_str)
        except ValueError:
            return {"error": f"Unknown event type: {event_type_str}"}

        event = MailEvent(
            team_id=team_id,
            from_agent=agent_id,
            to_agent=args.get("to", ""),
            event_type=evt_type,
            payload=args.get("payload") or {"message": args.get("message", "")},
        )
        sent = mailbox.send(event)
        return {"sent": True, "event_id": sent.event_id}

    if action == "broadcast":
        from agent_framework.models.team import MailEvent, MailEventType
        event = MailEvent(
            team_id=team_id,
            from_agent=agent_id,
            to_agent="*",
            event_type=MailEventType.BROADCAST_NOTICE,
            payload=args.get("payload") or {"message": args.get("message", "")},
        )
        sent_list = mailbox.broadcast(event)
        return {"broadcast": True, "recipients": len(sent_list)}

    if action == "read":
        limit_val = args.get("limit") or None
        events = mailbox.read_inbox(agent_id, limit=limit_val)
        return {
            "messages": [
                {
                    "event_id": e.event_id,
                    "from": e.from_agent,
                    "type": e.event_type.value,
                    "payload": e.payload,
                }
                for e in events
            ],
            "count": len(events),
        }

    if action == "ack":
        event_id = args.get("event_id", "")
        if not event_id:
            return {"error": "event_id required"}
        mailbox.ack(agent_id, event_id)
        return {"acked": True}

    if action == "reply":
        event_id = args.get("event_id", "")
        if not event_id:
            return {"error": "event_id required for reply"}
        reply_event = mailbox.reply(
            event_id, args.get("payload") or {"message": args.get("message", "")},
            source=agent_id,
            event_type=args.get("event_type"),  # Explicit override if provided
        )
        return {"replied": True, "event_id": reply_event.event_id}

    if action == "publish":
        topic = args.get("topic", "")
        if not topic:
            return {"error": "topic required for publish"}
        sent_list = mailbox.publish(
            topic, args.get("payload") or {"message": args.get("message", "")},
            source=agent_id, team_id=team_id,
        )
        return {"published": True, "recipients": len(sent_list)}

    if action == "subscribe":
        pattern = args.get("topic_pattern", "")
        if not pattern:
            return {"error": "topic_pattern required for subscribe"}
        mailbox.subscribe(agent_id, pattern)
        return {"subscribed": True, "pattern": pattern}

    if action == "unsubscribe":
        pattern = args.get("topic_pattern", "")
        if not pattern:
            return {"error": "topic_pattern required for unsubscribe"}
        mailbox.unsubscribe(agent_id, pattern)
        return {"unsubscribed": True, "pattern": pattern}

    return {"error": f"Unknown mail action: {action}"}
