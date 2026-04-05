"""Built-in control tools — framework/integration-layer bridging.

Category: control — blocked for sub-agents by default.

These tools provide control-plane capabilities:
- slash_command: execute integration-layer slash commands
- exit_plan_mode: transition from planning to execution

These are closer to integration-layer signals than core agent capabilities.
They are optional and may not be relevant in all deployment modes.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# Injected by entry.py or integration layer
_command_handler_ref: Any = None


def set_command_handler(handler: Any) -> None:
    """Bind the slash command handler (called by integration layer)."""
    global _command_handler_ref
    _command_handler_ref = handler


@tool(
    name="slash_command",
    description=(
        "Execute a framework or integration-layer slash command. "
        "Available commands depend on the deployment configuration."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "command"],
    namespace=SYSTEM_NAMESPACE,
)
def slash_command(command: str) -> dict:
    """Execute a slash command.

    Args:
        command: The slash command string (e.g. '/help', '/status').

    Returns:
        Dict with 'command', 'success', and 'output'.
    """
    if _command_handler_ref is None:
        return {
            "command": command,
            "success": False,
            "output": "No command handler configured",
        }

    try:
        result = _command_handler_ref(command)
        return {
            "command": command,
            "success": True,
            "output": str(result) if result is not None else "OK",
        }
    except Exception as e:
        return {
            "command": command,
            "success": False,
            "output": f"Command failed: {e}",
        }


# Plan mode state
_plan_mode_active: bool = False
_plan_mode_callback: Any = None


def set_plan_mode(active: bool, callback: Any = None) -> None:
    """Set plan mode state (called by integration layer)."""
    global _plan_mode_active, _plan_mode_callback
    _plan_mode_active = active
    _plan_mode_callback = callback


@tool(
    name="exit_plan_mode",
    description=(
        "Submit the implementation plan and exit planning mode. "
        "Only available when plan mode is active."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "plan"],
    namespace=SYSTEM_NAMESPACE,
)
def exit_plan_mode(plan: str) -> dict:
    """Exit plan mode and submit the plan.

    Args:
        plan: The implementation plan to submit.

    Returns:
        Dict with 'success' and 'message'.
    """
    if not _plan_mode_active:
        return {
            "success": False,
            "message": "Plan mode is not active",
        }

    if _plan_mode_callback is not None:
        try:
            _plan_mode_callback(plan)
        except Exception as e:
            return {
                "success": False,
                "message": f"Plan submission failed: {e}",
            }

    return {
        "success": True,
        "message": "Plan submitted successfully",
        "plan_length": len(plan),
    }
