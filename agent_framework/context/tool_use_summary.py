"""Tool use summary renderer — template-based tool result summarization for context compaction.

When compressing context (SNIP strategy), tool results from compactable tools
are replaced with rendered summary templates instead of blind head+tail truncation.
This preserves the semantic intent of tool calls while drastically reducing tokens.

Only tools in COMPACTABLE_TOOLS participate in template-based summarization.
Other tools fall back to the existing head+tail SNIP strategy.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_framework.protocols.core import ToolRegistryProtocol

# Single authority for which tools participate in template-based compaction.
# Aligns with Claude Code's COMPACTABLE_TOOLS set (microCompact.ts).
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_many_files",
    "bash_exec",
    "grep_search",
    "glob_files",
    "web_fetch",
    "web_search",
    "edit_file",
    "write_file",
})

# Placeholder for time-based clearing of aged tool results.
CLEARED_MESSAGE = "[Old tool result content cleared]"


class ToolUseSummaryRenderer:
    """Renders template-based summaries for tool results during compaction.

    Uses ToolMeta.tool_use_summary_tpl as a format string, interpolated
    with the tool's input arguments. Falls back gracefully when:
    - Tool is not in COMPACTABLE_TOOLS
    - Template is empty
    - Template references keys not in args (uses "..." placeholder)
    - Registry is unavailable
    """

    def __init__(self, registry: ToolRegistryProtocol | None = None) -> None:
        self._registry = registry

    def render(self, tool_name: str, tool_args: dict) -> str | None:
        """Render a summary for a tool result.

        Args:
            tool_name: The bare name of the tool.
            tool_args: The tool's input arguments dict.

        Returns:
            Rendered summary string, or None if not applicable (caller
            should fall back to head+tail truncation).
        """
        if tool_name not in COMPACTABLE_TOOLS:
            return None

        if self._registry is None:
            return None

        try:
            entry = self._registry.get_tool(tool_name)
        except KeyError:
            return None

        tpl = entry.meta.tool_use_summary_tpl
        if not tpl:
            return None

        # Render with defaultdict fallback for missing keys
        safe_args = defaultdict(lambda: "...", tool_args)
        try:
            rendered = tpl.format_map(safe_args)
        except (KeyError, ValueError, IndexError):
            return None

        return rendered
