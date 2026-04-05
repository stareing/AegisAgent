"""Deferred tool loading -- tools discovered on demand via tool_search.

Tools marked with should_defer=True in ToolMeta are not included in the
LLM's tool list. Instead, they can be discovered via the tool_search
meta-tool, which searches by keyword/description.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger

if TYPE_CHECKING:
    from agent_framework.protocols.core import ToolRegistryProtocol

logger = get_logger(__name__)


class DeferredToolManager:
    """Manages deferred tools that are discoverable but not initially visible.

    Deferred tools (should_defer=True) are excluded from export_schemas()
    by default. They can be discovered via search() and temporarily promoted
    to active status for the remainder of a run.
    """

    def __init__(self, registry: ToolRegistryProtocol) -> None:
        self._registry = registry
        self._promoted: set[str] = set()

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search deferred tools by keyword match on name and description.

        Args:
            query: Search keyword (case-insensitive).
            max_results: Maximum number of results to return.

        Returns:
            List of tool schemas in OpenAI function-calling format.
        """
        query_lower = query.lower()
        all_tools = self._registry.list_tools()
        matches: list[dict] = []

        for entry in all_tools:
            if not entry.meta.should_defer:
                continue

            name_match = query_lower in entry.meta.name.lower()
            desc_match = query_lower in entry.meta.description.lower()
            # v4.1: Also match on search_hint keywords
            hint_match = (
                query_lower in entry.meta.search_hint.lower()
                if entry.meta.search_hint else False
            )

            if name_match or desc_match or hint_match:
                schema: dict = {
                    "type": "function",
                    "function": {
                        "name": entry.meta.name,
                        "description": entry.meta.description,
                    },
                }
                if entry.meta.parameters_schema:
                    schema["function"]["parameters"] = entry.meta.parameters_schema
                matches.append(schema)

            if len(matches) >= max_results:
                break

        logger.info(
            "deferred_tool.search",
            query=query,
            results_count=len(matches),
        )
        return matches

    def promote(self, tool_name: str) -> None:
        """Temporarily promote a deferred tool to active visibility.

        After promotion, the tool will appear in export_schemas() for
        the remainder of the current run.

        Args:
            tool_name: The bare name of the tool to promote.

        Raises:
            KeyError: If the tool is not found in the registry.
        """
        entry = self._registry.get_tool(tool_name)
        if not entry.meta.should_defer:
            logger.debug("deferred_tool.already_active", tool=tool_name)
            return

        self._promoted.add(tool_name)
        logger.info("deferred_tool.promoted", tool=tool_name)

    def is_promoted(self, tool_name: str) -> bool:
        """Check if a deferred tool has been promoted."""
        return tool_name in self._promoted

    @property
    def promoted_tools(self) -> frozenset[str]:
        """Return the set of promoted tool names."""
        return frozenset(self._promoted)

    def reset(self) -> None:
        """Clear all promotions (e.g. between runs)."""
        self._promoted.clear()
