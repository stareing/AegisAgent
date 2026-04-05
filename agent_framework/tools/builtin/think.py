"""Built-in think tool — no-op reasoning aid.

Allows the agent to log a structured thought without taking any action.
Useful for complex reasoning, brainstorming, or planning before acting.
"""

from __future__ import annotations

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


@tool(
    name="think",
    description=(
        "Log a thought for reasoning, brainstorming, or planning. "
        "This tool does not take any action or obtain new information. "
        "Use it when complex reasoning is needed before deciding what to do."
    ),
    category="reasoning",
    require_confirm=False,
    tags=["system", "reasoning"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="think reason plan internally",
)
def think(thought: str) -> str:
    """Log a thought without taking any action.

    Args:
        thought: Your reasoning, analysis, or plan.

    Returns:
        Confirmation that the thought was logged.
    """
    return "Your thought has been logged."
