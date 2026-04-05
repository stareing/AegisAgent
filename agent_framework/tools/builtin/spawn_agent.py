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

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message
from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

if TYPE_CHECKING:
    from agent_framework.tools.executor import ToolExecutor

logger = get_logger(__name__)


@tool(
    name="spawn_agent",
    description=(
        "Spawn a sub-agent for a sub-task. "
        "mode='EPHEMERAL' (default): one-shot, destroyed after completion. "
        "mode='LONG_LIVED': agent stays alive — use send_message(spawn_id) for follow-ups. "
        "wait=true (default): blocks until done. wait=false: returns spawn_id, collect later with check_spawn_result(batch_pull=true)."
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


async def execute_spawn_agent(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute spawn_agent via ToolExecutor-owned dependencies/state."""
    from agent_framework.context.builder import ContextBuilder
    from agent_framework.models.subagent import (MemoryScope, SpawnContextMode,
                                                 SpawnMode, SubAgentSpec)
    from agent_framework.subagent.delegation import DelegationExecutor

    mode_str = args.get("mode", "ephemeral").upper()
    scope_str = args.get("memory_scope", "isolated").upper()
    context_mode_str = args.get("context_mode", "minimal").upper()
    wait = args.get("wait", True)
    llm_explicitly_async = "wait" in args and not args["wait"]
    if executor._progressive_mode and not llm_explicitly_async:
        wait = True

    parent_agent = executor._parent_agent_getter() if executor._parent_agent_getter else None
    parent_run_id = executor._current_run_id
    if not parent_run_id and parent_agent and hasattr(parent_agent, "agent_id"):
        parent_run_id = parent_agent.agent_id

    context_mode = (
        SpawnContextMode(context_mode_str)
        if context_mode_str in SpawnContextMode.__members__
        else SpawnContextMode.MINIMAL
    )
    spec = SubAgentSpec(
        parent_run_id=parent_run_id,
        task_input=args.get("task_input", ""),
        mode=SpawnMode(mode_str) if mode_str in SpawnMode.__members__ else SpawnMode.EPHEMERAL,
        skill_id=args.get("skill_id"),
        tool_category_whitelist=args.get("tool_categories"),
        context_mode=context_mode,
        memory_scope=MemoryScope(scope_str) if scope_str in MemoryScope.__members__ else MemoryScope.ISOLATED,
        token_budget=int(args.get("token_budget", 4096)),
        max_iterations=int(args.get("max_iterations", 10)),
        deadline_ms=int(args.get("deadline_ms", 0)),
    )
    if spec.context_seed is None:
        builder = ContextBuilder()
        if context_mode == SpawnContextMode.MINIMAL:
            spec.context_seed = [Message(role="user", content=spec.task_input)]
        else:
            spec.context_seed = builder.build_filtered_spawn_seed(
                session_messages=executor._current_session_messages,
                query=spec.task_input,
                token_budget=spec.token_budget,
            )

    parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"
    logger.info(
        "tool.routing.subagent",
        task_input=spec.task_input[:150],
        mode=mode_str,
        memory_scope=scope_str,
        wait=wait,
        parent_agent_id=parent_id,
    )

    if not wait:
        spawn_id = await executor._delegation.delegate_to_subagent_async(spec, parent_agent)
        logger.info("tool.routing.subagent.async_submitted", spawn_id=spawn_id)

        label = args.get("label", "") or f"Agent {spawn_id[:8]}"
        strategy_str = args.get("collection_strategy", "").upper()
        if strategy_str not in ("SEQUENTIAL", "BATCH_ALL", "HYBRID"):
            strategy_str = executor._default_collection_strategy
        ensure_lead_collector(executor, strategy_str)
        executor._lead_collector.register_spawn(spawn_id, spec.task_input, label)

        return {
            "spawn_id": spawn_id,
            "status": "PENDING",
            "label": label,
            "collection_strategy": strategy_str,
            "message": (
                "Sub-agent started asynchronously. Use check_spawn_result to collect the result. "
                f"Collection strategy: {strategy_str}. Use batch_pull=true for batch collection."
            ),
        }

    result = await executor._delegation.delegate_to_subagent(spec, parent_agent)
    logger.info(
        "tool.routing.subagent.done",
        spawn_id=result.spawn_id,
        success=result.success,
        iterations_used=result.iterations_used,
        answer_preview=(result.final_answer or result.error or "")[:120],
    )
    summary = DelegationExecutor.summarize_result(result).model_dump()
    if spec.mode == SpawnMode.LONG_LIVED:
        summary["spawn_id"] = result.spawn_id
        summary["mode"] = "LONG_LIVED"
        summary["hint"] = (
            "Agent is now IDLE. Use send_message(spawn_id='{}', message='...') "
            "to continue the conversation. Use close_agent(spawn_id='{}') when done."
        ).format(result.spawn_id, result.spawn_id)
    return summary


@tool(
    name="check_spawn_result",
    description=(
        "Collect results from async sub-agents spawned with wait=false. "
        "batch_pull=true: returns all completed results per collection_strategy. "
        "batch_pull=false: check a single spawn_id."
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


async def execute_check_spawn_result(executor: ToolExecutor, args: dict) -> dict:
    """Execute check_spawn_result via ToolExecutor-owned dependencies/state."""
    from agent_framework.subagent.delegation import DelegationExecutor

    batch_pull = args.get("batch_pull", False)
    if batch_pull and executor._lead_collector is None:
        return {
            "error": "No async agents spawned in this run. "
            "Use spawn_agent(wait=false) first, then check_spawn_result(batch_pull=true).",
            "results": [],
            "total_spawned": 0,
        }
    if batch_pull and executor._lead_collector is not None:
        async def _collect_fn(sid: str, wait: bool = False) -> dict | None:
            result = await executor._delegation.collect_subagent_result(sid, wait=wait)
            if result is None:
                return {"spawn_id": sid, "status": "RUNNING", "_still_running": True}
            return DelegationExecutor.summarize_result(result).model_dump()

        batch_result = await executor._lead_collector.pull(_collect_fn)
        return batch_result.model_dump()

    spawn_id = args.get("spawn_id", "")
    wait = args.get("wait", True)
    result = await executor._delegation.collect_subagent_result(spawn_id, wait=wait)
    if result is None:
        return {"spawn_id": spawn_id, "status": "RUNNING"}
    return DelegationExecutor.summarize_result(result).model_dump()


@tool(
    name="send_message",
    description=(
        "Send a follow-up message to a LONG_LIVED sub-agent. "
        "The agent sees its full prior conversation + your new message, runs, and returns to IDLE. "
        "Returns the agent's response directly (no need for check_spawn_result)."
    ),
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "subagent"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def send_message(
    spawn_id: str,
    message: str,
) -> dict:
    """Send a message to a LONG_LIVED sub-agent.

    Args:
        spawn_id: The spawn_id from a previous spawn_agent(mode='LONG_LIVED') call.
        message: The message to send. The agent sees its full conversation history + this.

    Returns:
        DelegationSummary dict with the agent's response.
    """
    raise RuntimeError(
        "send_message should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


async def execute_send_message(executor: ToolExecutor, args: dict) -> dict:
    """Execute send_message via ToolExecutor-owned dependencies/state."""
    from agent_framework.subagent.delegation import DelegationExecutor

    spawn_id = args.get("spawn_id", "")
    message = args.get("message", "")
    if not spawn_id or not message:
        return {"error": "spawn_id and message are required"}

    runtime = getattr(executor._delegation, "_sub_agent_runtime", None)
    if runtime is None:
        return {"error": "SubAgentRuntime not configured"}

    result = await runtime.send_message(spawn_id, message)
    return DelegationExecutor.summarize_result(result).model_dump()


@tool(
    name="close_agent",
    description="Close a LONG_LIVED sub-agent, releasing its resources. Use when done with a persistent agent.",
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "subagent"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def close_agent(
    spawn_id: str,
) -> dict:
    """Close a LONG_LIVED sub-agent.

    Args:
        spawn_id: The spawn_id of the LONG_LIVED agent to close.

    Returns:
        {"closed": true/false, "spawn_id": "..."}
    """
    raise RuntimeError(
        "close_agent should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


async def execute_close_agent(executor: ToolExecutor, args: dict) -> dict:
    """Execute close_agent via ToolExecutor-owned dependencies/state."""
    spawn_id = args.get("spawn_id", "")
    if not spawn_id:
        return {"error": "spawn_id is required"}

    runtime = getattr(executor._delegation, "_sub_agent_runtime", None)
    if runtime is None:
        return {"error": "SubAgentRuntime not configured", "closed": False}

    closed = runtime.close_live_agent(spawn_id)
    return {"spawn_id": spawn_id, "closed": closed}


@tool(
    name="resume_checkpoint",
    description=(
        "Resume a sub-agent from a saved checkpoint. "
        "Restores the exact conversation state and continues execution. "
        "Use after a crash or when a previously suspended agent needs to continue."
    ),
    category="delegation",
    require_confirm=False,
    tags=["system", "delegation", "subagent"],
    namespace=SYSTEM_NAMESPACE,
    source="subagent",
)
async def resume_checkpoint(
    spawn_id: str,
    checkpoint_id: str = "",
) -> dict:
    """Resume a sub-agent from a checkpoint.

    Args:
        spawn_id: The spawn_id of the sub-agent to resume.
        checkpoint_id: Specific checkpoint to resume from. If empty, uses the latest.

    Returns:
        DelegationSummary dict with the agent's response after resumption.
    """
    raise RuntimeError(
        "resume_checkpoint should not be called directly. "
        "It must be routed through the ToolExecutor."
    )


async def execute_resume_checkpoint(executor: ToolExecutor, args: dict) -> dict[str, Any]:
    """Execute resume_checkpoint via ToolExecutor-owned dependencies/state."""
    from agent_framework.subagent.delegation import DelegationExecutor

    spawn_id = args.get("spawn_id", "")
    checkpoint_id = args.get("checkpoint_id", "") or None
    if not spawn_id:
        return {"error": "spawn_id is required"}

    parent_agent = executor._parent_agent_getter() if executor._parent_agent_getter else None
    result = await executor._delegation.resume_from_checkpoint(
        spawn_id, parent_agent, checkpoint_id=checkpoint_id,
    )

    summary = DelegationExecutor.summarize_result(result).model_dump()
    summary["resumed_from_checkpoint"] = True
    summary["checkpoint_id"] = checkpoint_id or "latest"
    return summary


def ensure_lead_collector(executor: ToolExecutor, strategy_str: str) -> None:
    """Create LeadCollector on first async spawn if not exists."""
    if executor._lead_collector is not None:
        current = executor._lead_collector.strategy.value
        if strategy_str and strategy_str != current:
            logger.warning(
                "tool.lead_collector.strategy_mismatch",
                requested=strategy_str,
                active=current,
                hint="Collection strategy is set by the first async spawn and cannot change mid-run",
            )
        return

    from agent_framework.subagent.lead_collector import (CollectionStrategy,
                                                         LeadCollector)

    effective = strategy_str or executor._default_collection_strategy
    try:
        strategy = CollectionStrategy(effective)
    except ValueError:
        strategy = CollectionStrategy.HYBRID
    executor._lead_collector = LeadCollector(
        strategy=strategy,
        poll_interval_ms=executor._collection_poll_interval_ms,
    )
    logger.info(
        "tool.lead_collector.created",
        strategy=strategy.value,
        poll_interval_ms=executor._collection_poll_interval_ms,
    )
