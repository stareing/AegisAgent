"""Built-in tools: spawn_agent + check_spawn_result

Allows agents to spawn sub-agents for task delegation.
Registered as subagent::spawn_agent and subagent::check_spawn_result.

Three collection strategies for multi-agent orchestration:
- SEQUENTIAL (Mode A): collect one at a time, Lead decides after each
- BATCH_ALL (Mode B): wait for all, collect all at once
- HYBRID (Mode C, default): collect all currently-completed per pull

Two spawn modes:
- wait=True (default): synchronous — blocks until child completes
- wait=False: asynchronous — returns spawn_id immediately
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


@tool(
    name="spawn_agent",
    description=(
        "Spawn a sub-agent to handle a specific sub-task. "
        "Set wait=false to run asynchronously and collect later with check_spawn_result. "
        "When spawning multiple agents, set collection_strategy to control how results "
        "are collected: SEQUENTIAL (one at a time), BATCH_ALL (wait for all), "
        "or HYBRID (collect all completed per pull, default)."
    ),
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "subagent"],
    namespace=SYSTEM_NAMESPACE,
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
    collection_strategy: str = "HYBRID",
    label: str = "",
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
        wait: If True (default), block until sub-agent completes. If False, return spawn_id.
        collection_strategy: SEQUENTIAL, BATCH_ALL, or HYBRID (default). Controls how
            check_spawn_result collects results when multiple agents are spawned.
        label: Human-readable label for this agent (e.g. "Agent A — shell.py").

    Returns:
        wait=True: DelegationSummary dict.
        wait=False: {"spawn_id": "...", "status": "PENDING", "label": "..."} handle.
    """
    raise RuntimeError(
        "spawn_agent should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


@tool(
    name="check_spawn_result",
    description=(
        "Check or collect results of async sub-agents. Use after spawn_agent(wait=false). "
        "Set batch_pull=true to collect ALL currently-completed results at once "
        "(respects the collection_strategy set during spawn)."
    ),
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "subagent"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def check_spawn_result(
    spawn_id: str = "",
    wait: bool = True,
    batch_pull: bool = False,
) -> dict:
    """Check or wait for async sub-agent results.

    Args:
        spawn_id: The spawn_id from spawn_agent(wait=false). Required for single-agent check.
        wait: If True (default), block until completion. If False, return current status.
        batch_pull: If True, use the LeadCollector to pull results per the active
            collection strategy. Ignores spawn_id; pulls from the full spawn group.

    Returns:
        batch_pull=False: Single result — DelegationSummary or {"status": "RUNNING"}.
        batch_pull=True: BatchResult with results list, progress counters, batch_index.
    """
    raise RuntimeError(
        "check_spawn_result should not be called directly. "
        "It must be routed through the ToolExecutor."
    )
