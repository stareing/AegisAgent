"""Plugin protocol — interface contract for plugin implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agent_framework.models.plugin import PluginManifest

if TYPE_CHECKING:
    from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol
    from agent_framework.models.tool import ToolEntry


@runtime_checkable
class PluginProtocol(Protocol):
    """Contract every plugin must implement.

    Lifecycle: discover → validate → load → enable → disable → unload
    enable() failure must leave no half-registered state.
    """

    @property
    def manifest(self) -> PluginManifest: ...

    def load(self) -> None:
        """Import plugin module, validate dependencies."""
        ...

    def enable(self) -> None:
        """Register hooks, tools, agents into the framework."""
        ...

    def disable(self) -> None:
        """Unregister all extensions, release resources."""
        ...

    def unload(self) -> None:
        """Final cleanup, release module references."""
        ...

    def get_hooks(self) -> list[HookProtocol | AsyncHookProtocol]:
        """Return all hooks this plugin provides."""
        ...

    def get_tools(self) -> list[ToolEntry]:
        """Return all tool entries this plugin provides."""
        ...
