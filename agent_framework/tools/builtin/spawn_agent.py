"""Built-in tools: spawn_agent + check_spawn_result

Allows agents to spawn sub-agents for task delegation.
Registered as subagent::spawn_agent and subagent::check_spawn_result.

Two modes:
- wait=True (default): synchronous — blocks until child completes, returns DelegationSummary
- wait=False: asynchronous — returns spawn_id immediately, use check_spawn_result later
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool


@tool(
    name="spawn_agent",
    description=(
        "Spawn a sub-agent to handle a specific sub-task. "
        "Set wait=false to run asynchronously and collect the result later with check_spawn_result."
    ),
    category="subagent",
    require_confirm=False,
    source="subagent",
)
async def spawn_agent(
    task_input: str,
    mode: str = "EPHEMERAL",
    skill_id: str | None = None,
    tool_categories: list[str] | None = None,
    memory_scope: str = "ISOLATED",
    token_budget: int = 4096,
    max_iterations: int = 10,
    deadline_ms: int = 0,
    wait: bool = True,
) -> dict:
    """Spawn a sub-agent to handle a specific sub-task.

    Args:
        task_input: The task description for the sub-agent.
        mode: Spawn mode - EPHEMERAL, FORK, or LONG_LIVED.
        skill_id: Optional skill to activate in the sub-agent.
        tool_categories: Optional list of tool categories the sub-agent can use.
        memory_scope: Memory scope - ISOLATED, INHERIT_READ, or SHARED_WRITE.
        token_budget: Maximum token budget for child context seed.
        max_iterations: Child run max iterations.
        deadline_ms: Child execution deadline in milliseconds.
        wait: If True (default), block until sub-agent completes. If False, return spawn_id immediately.

    Returns:
        wait=True: DelegationSummary dict.
        wait=False: {"spawn_id": "...", "status": "PENDING"} handle.
    """
    raise RuntimeError(
        "spawn_agent should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


@tool(
    name="check_spawn_result",
    description="Check or collect the result of an async sub-agent. Use after spawn_agent(wait=false).",
    category="subagent",
    require_confirm=False,
    source="subagent",
)
async def check_spawn_result(
    spawn_id: str,
    wait: bool = True,
) -> dict:
    """Check or wait for an async sub-agent result.

    Args:
        spawn_id: The spawn_id returned by spawn_agent(wait=false).
        wait: If True (default), block until the sub-agent completes. If False, return current status.

    Returns:
        If complete: DelegationSummary dict with status, summary, artifacts.
        If still running (wait=False): {"spawn_id": "...", "status": "RUNNING"}.
    """
    raise RuntimeError(
        "check_spawn_result should not be called directly. "
        "It must be routed through the ToolExecutor."
    )
