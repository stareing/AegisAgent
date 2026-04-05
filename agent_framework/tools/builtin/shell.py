"""Built-in persistent shell tools.

Category: system — high-risk, requires explicit enablement.
Sub-agents: blocked by default.

Shell management logic lives in tools/shell/shell_manager.py.
This file contains only the thin tool interface definitions.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE
from agent_framework.tools.shell.shell_manager import (
    DEFAULT_TIMEOUT,
    ENV_WHITELIST,
    build_safe_env,
    check_banned,
)
from agent_framework.tools.shell.process_registry import ShellSessionManager

# Re-export for backward compat (old private names)
_build_safe_env = build_safe_env
_check_banned = check_banned
_ENV_WHITELIST = ENV_WHITELIST
_ShellSessionManager = ShellSessionManager

# Sandbox bridge — set by entry.py when sandbox_auto_select=True
_sandbox_bridge: Any = None


def set_sandbox_bridge(bridge: Any) -> None:
    """Wire sandbox bridge at framework setup time."""
    global _sandbox_bridge
    _sandbox_bridge = bridge


__all__ = [
    "bash_exec", "bash_output", "bash_stop", "task_stop", "kill_shell",
    "ShellSessionManager", "build_safe_env", "check_banned",
    "ENV_WHITELIST", "DEFAULT_TIMEOUT", "set_sandbox_bridge",
]


@tool(
    name="bash_exec",
    description=(
        "Execute a command in a persistent bash session. "
        "Working directory and environment persist across calls. "
        "Set run_in_background=True for long-running commands."
    ),
    category="system",
    require_confirm=True,
    tags=["system", "shell", "dangerous"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="run shell command bash terminal",
    activity_description="Running command",
    prompt=(
        "Execute a shell command. Use for system operations, builds, tests, and git commands. "
        "Avoid using for file operations that have dedicated tools."
    ),
    tool_use_summary_tpl="Ran command",
)
async def bash_exec(
    command: str,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    run_in_background: bool = False,
    description: str = "",
) -> dict:
    """Execute a shell command in a persistent bash session.

    Args:
        command: The shell command to execute.
        timeout_seconds: Maximum execution time (default 120s, max 600s).
        run_in_background: If True, run in background and return a task_id.
        description: Brief description of what the command does.

    Returns:
        Dict with 'output' and 'exit_code', or 'task_id' for background.
    """
    timeout_seconds = min(timeout_seconds, 600)

    banned_msg = check_banned(command)
    if banned_msg:
        return {"output": banned_msg, "exit_code": -2, "timed_out": False}

    session = ShellSessionManager.get("default")

    if run_in_background:
        task_id = await session.execute_background(command, timeout_seconds)
        return {"task_id": task_id, "status": "running"}

    # Route through sandbox bridge when enabled (risk-based sandbox selection)
    if _sandbox_bridge is not None:
        return await _sandbox_bridge.evaluate_and_execute(
            command, timeout_seconds=timeout_seconds, session=session,
        )

    return await session.execute(command, timeout_seconds)


@tool(
    name="bash_output",
    description=(
        "Get the output of a background bash command by task_id. "
        "Set block=True to wait for completion, or block=False for non-blocking check."
    ),
    category="system",
    require_confirm=False,
    tags=["system", "shell"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="get command output background",
    activity_description="Reading output",
)
async def bash_output(
    task_id: str,
    block: bool = False,
    timeout_ms: int = 30000,
) -> dict:
    """Get the result of a background bash command.

    Args:
        task_id: The task ID returned by bash_exec with run_in_background=True.
        block: If True, wait for the task to complete before returning.
        timeout_ms: Max wait time in milliseconds when block=True (default 30s).

    Returns:
        The command result if finished, or status 'running' if still executing.
    """
    import asyncio

    session = ShellSessionManager.get("default")

    if block:
        # Poll until done or timeout
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            result = session.get_background_result(task_id)
            if result is not None:
                return result
            await asyncio.sleep(0.2)
        # Timeout — return current status
        return {"status": "running", "task_id": task_id, "timed_out": True}

    result = session.get_background_result(task_id)
    if result is None:
        return {"status": "running", "task_id": task_id}
    return result


@tool(
    name="bash_stop",
    description="Stop a single background bash task by its task_id.",
    category="system",
    require_confirm=False,
    tags=["system", "shell"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="stop kill process",
)
def bash_stop(task_id: str) -> dict:
    """Stop a specific background task.

    Args:
        task_id: The task ID returned by bash_exec with run_in_background=True.

    Returns:
        The task result if already completed, or a cancelled confirmation.
    """
    session = ShellSessionManager.get("default")
    return session.stop_background_task(task_id)


@tool(
    name="task_stop",
    description=(
        "Stop a single background shell task by its task_id. "
        "This is a compatibility alias for bash_stop. "
        "Use only with task IDs returned by bash_exec(run_in_background=True)."
    ),
    category="system",
    require_confirm=False,
    tags=["system", "shell", "background", "task"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="stop kill background task",
)
def task_stop(task_id: str) -> dict:
    """Compatibility alias for stopping one background shell task.

    Args:
        task_id: The background task ID returned by bash_exec with
            run_in_background=True.

    Returns:
        The task result if already completed, or a cancelled confirmation.
    """
    return bash_stop(task_id)


@tool(
    name="kill_shell",
    description="Terminate the persistent shell session and all background tasks.",
    category="system",
    require_confirm=True,
    tags=["system", "shell", "dangerous"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="kill shell session",
)
async def kill_shell() -> str:
    """Kill the persistent shell process and all background tasks.

    Returns:
        Confirmation message.
    """
    session = ShellSessionManager.get("default")
    return await session.kill()
