"""Memory administration tools.

Category: memory_admin — NOT exposed to Agent by default.
Only available when CapabilityPolicy explicitly allows memory_admin category.
CLI slash commands route through ToolExecutor.execute() for unified execution.

These tools require a memory_manager dependency injected via ToolExecutor context.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# Injected at registration time by entry.py — not a global singleton.
_memory_manager_ref: Any = None
_agent_id_ref: str = ""


def set_memory_context(memory_manager: Any, agent_id: str) -> None:
    """Called by entry.py after setup to bind memory context."""
    global _memory_manager_ref, _agent_id_ref
    _memory_manager_ref = memory_manager
    _agent_id_ref = agent_id


def _get_manager() -> Any:
    if _memory_manager_ref is None:
        raise RuntimeError("Memory manager not initialized")
    return _memory_manager_ref


@tool(
    name="list_memories",
    description="List all saved memories for the current agent and user.",
    category="memory_admin",
    require_confirm=False,
    tags=["system", "memory"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="list show memories recall",
)
def list_memories(user_id: str | None = None) -> list[dict]:
    """List saved memories.

    Args:
        user_id: Optional user ID for namespace isolation.

    Returns:
        List of memory records as dicts.
    """
    manager = _get_manager()
    records = manager.list_memories(_agent_id_ref, user_id)
    return [
        {
            "memory_id": r.memory_id,
            "kind": r.kind.value,
            "title": r.title,
            "content": r.content[:200],
            "pinned": r.pinned if hasattr(r, "pinned") else r.is_pinned,
            "active": r.active if hasattr(r, "active") else r.is_active,
        }
        for r in records
    ]


@tool(
    name="forget_memory",
    description="Delete a saved memory by its memory_id.",
    category="memory_admin",
    require_confirm=True,
    tags=["system", "memory", "dangerous"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="delete forget remove memory",
)
def forget_memory(memory_id: str) -> str:
    """Delete one memory.

    Args:
        memory_id: The ID of the memory to delete.

    Returns:
        Confirmation message.
    """
    manager = _get_manager()
    manager.forget(memory_id)
    return f"Memory {memory_id} deleted"


@tool(
    name="clear_memories",
    description="Clear all saved memories for the current agent.",
    category="memory_admin",
    require_confirm=True,
    tags=["system", "memory", "dangerous"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="clear all memories reset",
)
def clear_memories(user_id: str | None = None) -> str:
    """Clear all memories.

    Args:
        user_id: Optional user ID for namespace isolation.

    Returns:
        Number of memories cleared.
    """
    manager = _get_manager()
    count = manager.clear_memories(_agent_id_ref, user_id)
    return f"Cleared {count} memories"
