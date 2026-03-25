"""Hierarchical /memory slash command for memory management.

Subcommands:
  show   - Display all memories formatted nicely
  add    - Save a new memory via ToolAction
  reload - Reload memories from store
  list   - List memory store info
  clear  - Clear all memories
  pin    - Pin a memory by ID
  unpin  - Unpin a memory by ID
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.commands.protocol import (
    CommandActionReturn,
    CommandContext,
    MessageAction,
    SlashCommand,
    ToolAction,
)

if TYPE_CHECKING:
    from agent_framework.entry import AgentFramework


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _get_framework(ctx: CommandContext) -> AgentFramework:
    """Extract the framework instance from command context."""
    fw = ctx.framework
    if fw is None:
        raise ValueError("Framework not available in command context")
    return fw


def _handle_show(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """Display all memories with kind, content, and pinned status."""
    fw = _get_framework(ctx)
    memories = fw.list_memories()

    if not memories:
        return MessageAction(content="No memories saved.")

    lines: list[str] = ["Saved memories:", ""]
    for i, mem in enumerate(memories, 1):
        kind = getattr(mem, "kind", "CUSTOM")
        content = getattr(mem, "content", str(mem))
        title = getattr(mem, "title", "")
        memory_id = getattr(mem, "memory_id", "?")
        pinned = getattr(mem, "is_pinned", False)
        active = getattr(mem, "is_active", True)

        pin_marker = " [pinned]" if pinned else ""
        active_marker = " (inactive)" if not active else ""
        title_part = f" — {title}" if title else ""

        lines.append(
            f"  {i}. [{kind}] {content}{title_part}{pin_marker}{active_marker}"
        )
        lines.append(f"     id: {memory_id}")

    lines.append("")
    lines.append(f"Total: {len(memories)} memories")
    return MessageAction(content="\n".join(lines))


def _handle_add(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Add a new memory. Returns a ToolAction targeting save_memory_tool."""
    text = args.strip()
    if not text:
        return MessageAction(
            message_type="error",
            content="Usage: /memory add <text>",
        )

    return ToolAction(
        tool_name="save_memory_tool",
        tool_args={"content": text, "kind": "CUSTOM"},
    )


def _handle_reload(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """Reload memories from the store and report count."""
    fw = _get_framework(ctx)
    memories = fw.list_memories()
    count = len(memories)
    return MessageAction(content=f"Reloaded {count} memories from store.")


def _handle_list(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """List memory store info and paths."""
    fw = _get_framework(ctx)

    lines: list[str] = ["Memory store info:", ""]

    # Report store type
    store = getattr(fw, "_memory_store", None)
    if store is not None:
        store_type = type(store).__name__
        db_path = getattr(store, "_db_path", None) or getattr(store, "db_path", None)
        lines.append(f"  Store type: {store_type}")
        if db_path:
            lines.append(f"  Database:   {db_path}")
    else:
        lines.append("  Store type: (not initialized)")

    # Memory count summary
    memories = fw.list_memories()
    pinned_count = sum(1 for m in memories if getattr(m, "is_pinned", False))
    active_count = sum(1 for m in memories if getattr(m, "is_active", True))

    lines.append(f"  Total:      {len(memories)}")
    lines.append(f"  Active:     {active_count}")
    lines.append(f"  Pinned:     {pinned_count}")

    return MessageAction(content="\n".join(lines))


def _handle_clear(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """Clear all memories."""
    fw = _get_framework(ctx)
    count = fw.clear_memories()
    return MessageAction(content=f"Cleared {count} memories.")


def _handle_pin(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Pin a memory by ID."""
    memory_id = args.strip()
    if not memory_id:
        return MessageAction(
            message_type="error",
            content="Usage: /memory pin <memory_id>",
        )

    fw = _get_framework(ctx)
    try:
        fw.pin_memory(memory_id)
    except Exception as exc:
        return MessageAction(
            message_type="error",
            content=f"Failed to pin memory '{memory_id}': {exc}",
        )
    return MessageAction(content=f"Memory '{memory_id}' pinned.")


def _handle_unpin(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Unpin a memory by ID."""
    memory_id = args.strip()
    if not memory_id:
        return MessageAction(
            message_type="error",
            content="Usage: /memory unpin <memory_id>",
        )

    fw = _get_framework(ctx)
    try:
        fw.unpin_memory(memory_id)
    except Exception as exc:
        return MessageAction(
            message_type="error",
            content=f"Failed to unpin memory '{memory_id}': {exc}",
        )
    return MessageAction(content=f"Memory '{memory_id}' unpinned.")


def _handle_memory_root(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Default handler when no subcommand given — show usage."""
    return MessageAction(
        content=(
            "Usage: /memory <subcommand>\n"
            "\n"
            "Subcommands:\n"
            "  show   - Display all saved memories\n"
            "  add    - Save a new memory: /memory add <text>\n"
            "  reload - Reload memories from store\n"
            "  list   - Show memory store info\n"
            "  clear  - Clear all memories\n"
            "  pin    - Pin a memory: /memory pin <id>\n"
            "  unpin  - Unpin a memory: /memory unpin <id>"
        ),
    )


# ---------------------------------------------------------------------------
# Exported SlashCommand instance
# ---------------------------------------------------------------------------

memory_command = SlashCommand(
    name="memory",
    description="Manage saved memories (show, add, reload, list, clear, pin, unpin)",
    aliases=["mem"],
    category="memory",
    handler=_handle_memory_root,
    subcommands=[
        SlashCommand(
            name="show",
            description="Display all memories with kind, content, and pinned status",
            handler=_handle_show,
        ),
        SlashCommand(
            name="add",
            description="Save a new memory: /memory add <text>",
            handler=_handle_add,
        ),
        SlashCommand(
            name="reload",
            description="Reload memories from store and report count",
            handler=_handle_reload,
        ),
        SlashCommand(
            name="list",
            description="List memory store info and paths",
            aliases=["ls"],
            handler=_handle_list,
        ),
        SlashCommand(
            name="clear",
            description="Clear all memories",
            handler=_handle_clear,
        ),
        SlashCommand(
            name="pin",
            description="Pin a memory by ID",
            handler=_handle_pin,
        ),
        SlashCommand(
            name="unpin",
            description="Unpin a memory by ID",
            handler=_handle_unpin,
        ),
    ],
)
