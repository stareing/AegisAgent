"""Built-in persistent shell tools.

Category: system — high-risk, requires explicit enablement.
Sub-agents: blocked by default.

Shell management logic lives in tools/shell/shell_manager.py.
This file contains only the thin tool interface definitions.
"""

from __future__ import annotations

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

__all__ = [
    "bash_exec", "bash_output", "kill_shell",
    "ShellSessionManager", "build_safe_env", "check_banned",
    "ENV_WHITELIST", "DEFAULT_TIMEOUT",
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

    return await session.execute(command, timeout_seconds)


@tool(
    name="bash_output",
    description="Check the output of a background bash command by task_id.",
    category="system",
    require_confirm=False,
    tags=["system", "shell"],
    namespace=SYSTEM_NAMESPACE,
)
def bash_output(task_id: str) -> dict:
    """Get the result of a background bash command.

    Args:
        task_id: The task ID returned by bash_exec with run_in_background=True.

    Returns:
        The command result if finished, or status 'running' if still executing.
    """
    session = ShellSessionManager.get("default")
    result = session.get_background_result(task_id)
    if result is None:
        return {"status": "running", "task_id": task_id}
    return result


@tool(
    name="kill_shell",
    description="Terminate the persistent shell session and all background tasks.",
    category="system",
    require_confirm=True,
    tags=["system", "shell", "dangerous"],
    namespace=SYSTEM_NAMESPACE,
)
async def kill_shell() -> str:
    """Kill the persistent shell process and all background tasks.

    Returns:
        Confirmation message.
    """
    session = ShellSessionManager.get("default")
    return await session.kill()
