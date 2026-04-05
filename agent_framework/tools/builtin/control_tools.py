"""Built-in control tools — framework/integration-layer bridging.

Category: control — blocked for sub-agents by default.

These tools provide control-plane capabilities:
- slash_command: execute integration-layer slash commands
- enter_plan_mode: transition to read-only planning mode
- exit_plan_mode: submit plan and exit planning mode
- write_plan: write/update the plan file during plan mode

These are closer to integration-layer signals than core agent capabilities.
They are optional and may not be relevant in all deployment modes.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# Injected by entry.py or integration layer
_command_handler_ref: Any = None

# Plan mode controller reference (injected by entry.py)
_plan_mode_controller: Any = None
_plan_mode_agent_state_getter: Any = None
_plan_mode_on_enter: Any = None
_plan_mode_on_exit: Any = None


def set_command_handler(handler: Any) -> None:
    """Bind the slash command handler (called by integration layer)."""
    global _command_handler_ref
    _command_handler_ref = handler


def set_plan_mode_runtime(
    controller: Any,
    agent_state_getter: Any = None,
    on_enter: Any = None,
    on_exit: Any = None,
) -> None:
    """Bind plan mode controller and callbacks (called by entry.py/coordinator)."""
    global _plan_mode_controller, _plan_mode_agent_state_getter
    global _plan_mode_on_enter, _plan_mode_on_exit
    _plan_mode_controller = controller
    _plan_mode_agent_state_getter = agent_state_getter
    _plan_mode_on_enter = on_enter
    _plan_mode_on_exit = on_exit


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
    is_read_only=True,
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


# ---------------------------------------------------------------------------
# Plan mode tools (v4.0)
# ---------------------------------------------------------------------------

# Legacy plan mode state (kept for backward compat)
_plan_mode_active: bool = False
_plan_mode_callback: Any = None


def set_plan_mode(active: bool, callback: Any = None) -> None:
    """Set plan mode state (called by integration layer)."""
    global _plan_mode_active, _plan_mode_callback
    _plan_mode_active = active
    _plan_mode_callback = callback


@tool(
    name="enter_plan_mode",
    description=(
        "Enter planning mode. Restricts tools to read-only exploration. "
        "Creates a plan file for writing your implementation plan. "
        "Use when you need to design an approach before implementation."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "plan"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="enter planning mode read only",
)
def enter_plan_mode() -> dict:
    """Enter plan mode: restrict to read-only tools, create plan file.

    Returns:
        Dict with 'success', 'message', and 'plan_file_path'.
    """
    if _plan_mode_controller is None:
        return {
            "success": False,
            "message": "Plan mode controller not configured",
        }

    # Check if already in plan mode
    if _plan_mode_agent_state_getter:
        state = _plan_mode_agent_state_getter()
        if state and state.plan_mode_state and state.plan_mode_state.active:
            return {
                "success": False,
                "message": "Already in plan mode",
                "plan_file_path": state.plan_mode_state.plan_file_path,
            }

    try:
        if _plan_mode_on_enter:
            plan_state = _plan_mode_on_enter()
            return {
                "success": True,
                "message": "Entered plan mode. Tools restricted to read-only.",
                "plan_file_path": plan_state.plan_file_path if plan_state else None,
            }
        return {"success": False, "message": "No enter callback configured"}
    except Exception as e:
        return {"success": False, "message": f"Failed to enter plan mode: {e}"}


@tool(
    name="exit_plan_mode",
    description=(
        "Submit the implementation plan and exit planning mode. "
        "Restores full tool access. Only available when plan mode is active."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "plan"],
    namespace=SYSTEM_NAMESPACE,
    search_hint="exit planning mode submit plan",
)
def exit_plan_mode(plan: str = "") -> dict:
    """Exit plan mode and submit the plan.

    Args:
        plan: The implementation plan to submit (optional if plan file exists).

    Returns:
        Dict with 'success' and 'message'.
    """
    # Legacy path
    if _plan_mode_active and _plan_mode_callback is not None:
        try:
            _plan_mode_callback(plan)
        except Exception as e:
            return {"success": False, "message": f"Plan submission failed: {e}"}
        return {"success": True, "message": "Plan submitted successfully", "plan_length": len(plan)}

    # v4.0 path — check if plan mode is actually active before exiting
    if _plan_mode_on_exit:
        # Verify plan mode is active via agent state
        is_active = False
        if _plan_mode_agent_state_getter:
            state = _plan_mode_agent_state_getter()
            if state and state.plan_mode_state and state.plan_mode_state.active:
                is_active = True
        if is_active:
            try:
                _plan_mode_on_exit(plan)
                return {
                    "success": True,
                    "message": "Exited plan mode. Full tool access restored.",
                    "plan_length": len(plan) if plan else 0,
                }
            except Exception as e:
                return {"success": False, "message": f"Failed to exit plan mode: {e}"}

    return {"success": False, "message": "Plan mode is not active"}


@tool(
    name="write_plan",
    description=(
        "Write or update the plan file during planning mode. "
        "Only available when plan mode is active."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "plan"],
    namespace=SYSTEM_NAMESPACE,
    search_hint="write plan file",
)
def write_plan(content: str) -> dict:
    """Write content to the plan file.

    Args:
        content: The plan content to write.

    Returns:
        Dict with 'success', 'message', and 'plan_file_path'.
    """
    if _plan_mode_controller is None:
        return {"success": False, "message": "Plan mode controller not configured"}

    if _plan_mode_agent_state_getter:
        state = _plan_mode_agent_state_getter()
        if not state or not state.plan_mode_state or not state.plan_mode_state.active:
            return {"success": False, "message": "Plan mode is not active"}

        try:
            path = _plan_mode_controller.write_plan(state.plan_mode_state, content)
            return {
                "success": True,
                "message": f"Plan written ({len(content)} chars)",
                "plan_file_path": path,
            }
        except Exception as e:
            return {"success": False, "message": f"Failed to write plan: {e}"}

    return {"success": False, "message": "No agent state available"}
