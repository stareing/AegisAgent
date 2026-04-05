"""Plugin tool factory — OC-style per-session tool creation with context injection.

Plugins register tool factories instead of static tool entries. At session
start, the factory is called with a PluginToolContext carrying session/agent
metadata. This enables tools that are scoped to a specific session, agent,
or workspace without leaking global state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agent_framework.models.tool import ToolEntry


# ---------------------------------------------------------------------------
# Context passed to tool factories at creation time
# ---------------------------------------------------------------------------

class PluginToolContext(BaseModel):
    """Trusted execution context injected into plugin tool factories.

    Frozen to prevent mutation after construction.
    """

    model_config = {"frozen": True}

    session_id: str = ""
    agent_id: str = ""
    run_id: str = ""
    workspace_dir: str = ""
    sandboxed: bool = False
    plugin_id: str = ""
    extra: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool factory protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PluginToolFactory(Protocol):
    """Contract for plugins that provide session-scoped tools.

    The factory is called once per session setup. Returned tools
    are registered into the scoped ToolRegistry for that session.
    """

    def create_tools(self, context: PluginToolContext) -> list[ToolEntry]:
        """Create tool entries scoped to the given session context."""
        ...


# ---------------------------------------------------------------------------
# Tool factory registration entry
# ---------------------------------------------------------------------------

class ToolFactoryEntry(BaseModel):
    """Registration record for a plugin tool factory."""

    model_config = {"frozen": True}

    plugin_id: str
    factory_id: str
    description: str = ""
    tool_names: list[str] = Field(
        default_factory=list,
        description="Expected tool names (for discovery before instantiation)",
    )
