"""Built-in system tools.

These tools provide system-level operations.
All require confirmation (require_confirm=True) and belong to the 'system' category,
which is blocked by default for sub-agents per section 20.1.
"""

from __future__ import annotations

import subprocess

from agent_framework.tools.decorator import tool


@tool(
    name="run_command",
    description="Execute a shell command and return its output. Use with caution.",
    category="system",
    require_confirm=True,
    tags=["dangerous"],
)
def run_command(
    command: str,
    timeout_seconds: int = 30,
    cwd: str | None = None,
) -> dict:
    """Execute a shell command.

    Args:
        command: The shell command to execute.
        timeout_seconds: Maximum execution time in seconds.
        cwd: Working directory for the command.

    Returns:
        Dict with stdout, stderr, and return_code.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout_seconds} seconds",
            "return_code": -1,
        }


@tool(
    name="get_env",
    description="Get the value of an environment variable.",
    category="system",
    require_confirm=False,
)
def get_env(name: str, default: str = "") -> str:
    """Get an environment variable value.

    Args:
        name: The environment variable name.
        default: Default value if not set.

    Returns:
        The environment variable value.
    """
    import os
    return os.environ.get(name, default)
