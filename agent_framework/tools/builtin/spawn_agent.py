"""Built-in tool: spawn_agent

Allows agents to spawn sub-agents for task delegation.
Registered as subagent::spawn_agent in the tool catalog.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool


@tool(
    name="spawn_agent",
    description="Spawn a sub-agent to handle a specific sub-task. The sub-agent runs independently and returns a result.",
    category="subagent",
    require_confirm=False,
)
async def spawn_agent(
    task_input: str,
    mode: str = "EPHEMERAL",
    skill_id: str | None = None,
    tool_categories: list[str] | None = None,
    memory_scope: str = "ISOLATED",
) -> dict:
    """Spawn a sub-agent to handle a specific sub-task.

    Args:
        task_input: The task description for the sub-agent.
        mode: Spawn mode - EPHEMERAL, FORK, or LONG_LIVED.
        skill_id: Optional skill to activate in the sub-agent.
        tool_categories: Optional list of tool categories the sub-agent can use.
        memory_scope: Memory scope - ISOLATED, INHERIT_READ, or SHARED_WRITE.

    Returns:
        DelegationSummary dict (not full trace, per section 14.6).
    """
    # Schema placeholder — actual execution routed through ToolExecutor -> DelegationExecutor
    raise RuntimeError(
        "spawn_agent should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


# Override source to "subagent" so ToolExecutor routes correctly
spawn_agent.__tool_meta__.source = "subagent"  # type: ignore[attr-defined]
