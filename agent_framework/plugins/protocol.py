"""Plugin protocol — interface contract for plugin implementations.

Extends the base lifecycle with OC-compatible registration methods
for providers, channels, and tool factories.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_framework.models.plugin import PluginManifest

if TYPE_CHECKING:
    from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol
    from agent_framework.models.tool import ToolEntry
    from agent_framework.plugins.tool_factory import PluginToolFactory


@runtime_checkable
class PluginProtocol(Protocol):
    """Contract every plugin must implement.

    Lifecycle: discover → validate → load → enable → disable → unload
    enable() failure must leave no half-registered state.

    Extension types:
    - hooks: pre/post lifecycle gates and observers
    - tools: agent-callable tool entries
    - tool_factories: session-scoped tool creators (OC-compatible)
    - commands: interactive CLI commands (registered to SkillRouter)
    - agents: agent templates / sub-agent definitions
    - providers: model provider definitions (OC-compatible)
    - channels: messaging channel definitions (OC-compatible)
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

    def get_tool_factories(self) -> list[PluginToolFactory]:
        """Return tool factories for session-scoped tool creation (OC-compatible).

        Tool factories are called at session start with PluginToolContext.
        Returns empty list if plugin provides no tool factories.
        """
        ...

    def get_commands(self) -> list[Any]:
        """Return command/skill definitions this plugin provides.

        Each entry should be a Skill-compatible dict or Skill instance
        that can be registered via SkillRouter.register_skill().
        Returns empty list if plugin provides no commands.
        """
        ...

    def get_agents(self) -> list[Any]:
        """Return agent template definitions this plugin provides.

        Each entry should be an AgentConfig-compatible dict or object.
        Returns empty list if plugin provides no agent templates.
        """
        ...

    def get_providers(self) -> list[Any]:
        """Return model provider definitions this plugin provides (OC-compatible).

        Each entry should describe a model adapter/provider configuration.
        Returns empty list if plugin provides no providers.
        """
        ...

    def get_channels(self) -> list[Any]:
        """Return messaging channel definitions this plugin provides (OC-compatible).

        Each entry should describe a messaging channel integration.
        Returns empty list if plugin provides no channels.
        """
        ...
