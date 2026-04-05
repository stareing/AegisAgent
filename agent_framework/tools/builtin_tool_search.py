"""Built-in tool_search meta-tool for deferred tool discovery.

Allows the LLM to search for tools that are not in its immediate tool list.
Found tools can be promoted to active status for the remainder of the run.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.tools.decorator import tool

logger = get_logger(__name__)

# Module-level reference set during framework setup.
_deferred_manager: Any = None


def set_deferred_manager(manager: Any) -> None:
    """Wire the DeferredToolManager reference. Called once during framework setup."""
    global _deferred_manager
    _deferred_manager = manager


@tool(
    name="tool_search",
    description=(
        "Search for additional tools by keyword. "
        "Use this when the current tool set does not contain a tool "
        "needed for the task. Returns matching tool schemas. "
        "After finding a useful tool, it will be made available for use."
    ),
    category="meta",
    require_confirm=False,
    is_read_only=True,
    always_load=True,
    search_hint="search find discover tools",
)
def tool_search(query: str, max_results: int = 5) -> str:
    """Search for deferred tools by keyword and promote matches.

    Args:
        query: Keyword to search tool names and descriptions.
        max_results: Maximum number of results to return.

    Returns:
        JSON string of matching tool schemas, or an error message.
    """
    if _deferred_manager is None:
        return json.dumps({"error": "Tool search is not configured."})

    results = _deferred_manager.search(query, max_results=max_results)

    # Auto-promote found tools so they become available immediately
    for schema in results:
        tool_name = schema.get("function", {}).get("name", "")
        if tool_name:
            try:
                _deferred_manager.promote(tool_name)
            except KeyError:
                logger.warning("tool_search.promote_failed", tool=tool_name)

    if not results:
        return json.dumps({"message": f"No tools found matching '{query}'.", "results": []})

    return json.dumps({"results": results, "count": len(results)})
