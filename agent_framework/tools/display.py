"""Tool display — UI metadata for tool presentation.

Maps tool names to display specs (emoji, title, label, detail keys)
for consistent rendering across terminal, web, and TUI interfaces.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolDisplaySpec(BaseModel):
    """Display specification for a tool."""

    model_config = {"frozen": True}

    emoji: str = "🔧"
    title: str = ""
    label: str = ""
    detail_keys: list[str] = Field(default_factory=list)


# Built-in tool display mappings
BUILTIN_TOOL_DISPLAY: dict[str, ToolDisplaySpec] = {
    "read_file": ToolDisplaySpec(emoji="📄", title="Read File", label="Reading", detail_keys=["path"]),
    "write_file": ToolDisplaySpec(emoji="✏️", title="Write File", label="Writing", detail_keys=["path"]),
    "edit_file": ToolDisplaySpec(emoji="📝", title="Edit File", label="Editing", detail_keys=["path"]),
    "code_edit": ToolDisplaySpec(emoji="📝", title="Code Edit", label="Editing", detail_keys=["path", "operation"]),
    "list_dir": ToolDisplaySpec(emoji="📁", title="List Directory", label="Listing", detail_keys=["path"]),
    "search_files": ToolDisplaySpec(emoji="🔍", title="Search Files", label="Searching", detail_keys=["pattern", "path"]),
    "search_content": ToolDisplaySpec(emoji="🔎", title="Search Content", label="Searching", detail_keys=["query", "path"]),
    "shell": ToolDisplaySpec(emoji="💻", title="Shell", label="Running", detail_keys=["command"]),
    "spawn_agent": ToolDisplaySpec(emoji="🤖", title="Spawn Agent", label="Spawning", detail_keys=["task_input"]),
    "think": ToolDisplaySpec(emoji="💭", title="Think", label="Thinking", detail_keys=[]),
    "web_search": ToolDisplaySpec(emoji="🌐", title="Web Search", label="Searching", detail_keys=["query"]),
    "web_fetch": ToolDisplaySpec(emoji="🌍", title="Web Fetch", label="Fetching", detail_keys=["url"]),
    "memory_admin": ToolDisplaySpec(emoji="🧠", title="Memory", label="Managing", detail_keys=["action"]),
    "invoke_skill": ToolDisplaySpec(emoji="⚡", title="Invoke Skill", label="Invoking", detail_keys=["skill_id"]),
    "task_manager": ToolDisplaySpec(emoji="📋", title="Task Manager", label="Managing", detail_keys=["action"]),
    "team": ToolDisplaySpec(emoji="👥", title="Team", label="Coordinating", detail_keys=["action"]),
    "mail": ToolDisplaySpec(emoji="📬", title="Mail", label="Messaging", detail_keys=["action"]),
}

# Custom overrides (loaded from config or plugin)
_custom_overrides: dict[str, ToolDisplaySpec] = {}


def register_display(tool_name: str, spec: ToolDisplaySpec) -> None:
    """Register a custom tool display spec (plugins can override builtins)."""
    _custom_overrides[tool_name] = spec


def resolve_tool_display(tool_name: str) -> ToolDisplaySpec:
    """Resolve display spec for a tool with fallback chain.

    Priority: custom override -> builtin -> default.
    """
    if tool_name in _custom_overrides:
        return _custom_overrides[tool_name]
    if tool_name in BUILTIN_TOOL_DISPLAY:
        return BUILTIN_TOOL_DISPLAY[tool_name]
    # Default: use tool name as title
    return ToolDisplaySpec(title=tool_name.replace("_", " ").title(), label=tool_name)


def format_tool_detail(
    tool_name: str,
    params: dict,
    max_entries: int = 8,
) -> str:
    """Format tool parameters for UI display.

    Extracts key parameters defined in the display spec and formats
    them as a compact string.
    """
    spec = resolve_tool_display(tool_name)
    if not spec.detail_keys:
        return ""

    parts: list[str] = []
    for key in spec.detail_keys[:max_entries]:
        value = params.get(key)
        if value is not None:
            # Truncate long values
            text = str(value)
            if len(text) > 80:
                text = text[:77] + "..."
            parts.append(f"{key}={text}")

    return ", ".join(parts)
