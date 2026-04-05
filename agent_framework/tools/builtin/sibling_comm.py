"""Built-in tools for sibling sub-agent communication.

Allows sub-agents to send/receive messages to/from siblings under the same
parent run, enabling direct coordination without parent relay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

if TYPE_CHECKING:
    from agent_framework.tools.executor import ToolExecutor

logger = get_logger(__name__)


@tool(
    name="sibling_send",
    description=(
        "Send a message to a sibling sub-agent by spawn_id. "
        "Only works between agents under the same parent run."
    ),
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "sibling"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def sibling_send(
    to_spawn_id: str,
    message: str,
    payload: dict | None = None,
) -> dict:
    """Send a message to a sibling sub-agent."""
    raise RuntimeError("sibling_send must be routed through ToolExecutor.")


async def execute_sibling_send(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute sibling_send via ToolExecutor."""
    channel = getattr(executor, "_sibling_channel", None)
    if channel is None:
        return {"error": "Sibling communication not configured"}

    from_spawn_id = getattr(executor, "_current_spawn_id", "")
    parent_run_id = executor._current_run_id
    to_spawn_id = args.get("to_spawn_id", "")
    message = args.get("message", "")

    if not to_spawn_id or not message:
        return {"error": "to_spawn_id and message are required"}

    msg = channel.send(
        from_spawn_id=from_spawn_id,
        to_spawn_id=to_spawn_id,
        parent_run_id=parent_run_id,
        content=message,
        payload=args.get("payload"),
    )
    return {
        "message_id": msg.message_id,
        "sent": True,
        "to": to_spawn_id,
    }


@tool(
    name="sibling_receive",
    description="Receive unread messages from sibling sub-agents.",
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "sibling"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def sibling_receive() -> dict:
    """Receive messages from siblings."""
    raise RuntimeError("sibling_receive must be routed through ToolExecutor.")


async def execute_sibling_receive(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute sibling_receive via ToolExecutor."""
    channel = getattr(executor, "_sibling_channel", None)
    if channel is None:
        return {"error": "Sibling communication not configured", "messages": []}

    spawn_id = getattr(executor, "_current_spawn_id", "")
    parent_run_id = executor._current_run_id

    messages = channel.receive(spawn_id, parent_run_id)
    return {
        "messages": [
            {
                "message_id": m.message_id,
                "from": m.from_spawn_id,
                "content": m.content,
                "payload": m.payload,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "count": len(messages),
    }
