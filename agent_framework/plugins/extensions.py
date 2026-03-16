"""PluginExtensionRegistrar — unified apply/rollback for plugin extensions.

Ensures symmetric register/unregister for hooks, tools, commands, and agents.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger
from agent_framework.hooks.registry import HookRegistry

logger = get_logger(__name__)


class PluginApplyReceipt(BaseModel):
    """Record of what was registered — used for symmetric rollback."""

    plugin_id: str = ""
    hook_ids: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    agent_template_count: int = 0


class PluginExtensionRegistrar:
    """Applies and rolls back plugin extensions atomically.

    Single responsibility: register/unregister extensions into their
    respective registries. Does NOT handle plugin lifecycle state.
    """

    def __init__(
        self,
        hook_registry: HookRegistry,
        tool_registry: Any = None,
        skill_router: Any = None,
    ) -> None:
        self._hooks = hook_registry
        self._tool_registry = tool_registry
        self._skill_router = skill_router
        self._agent_templates: dict[str, list] = {}

    def apply(self, plugin: Any) -> PluginApplyReceipt:
        """Register all extensions from a plugin. Returns receipt for rollback.

        On failure the partial receipt is attached to the exception as
        ``partial_receipt`` so the caller can roll back what was registered.
        """
        pid = plugin.manifest.plugin_id
        receipt = PluginApplyReceipt(plugin_id=pid)

        try:
            # 1. Hooks
            for hook in plugin.get_hooks():
                self._hooks.register(hook)
                receipt.hook_ids.append(hook.meta.hook_id)

            # 2. Tools
            tools = plugin.get_tools()
            if tools and self._tool_registry is not None:
                for tool_entry in tools:
                    self._tool_registry.register(tool_entry)
                    receipt.tool_names.append(tool_entry.meta.name)

            # 3. Commands (skills)
            if hasattr(plugin, "get_commands"):
                commands = plugin.get_commands()
                if commands and self._skill_router is not None:
                    for cmd in commands:
                        self._skill_router.register_skill(cmd)
                        sid = getattr(cmd, "skill_id", None) or str(cmd)
                        receipt.skill_ids.append(sid)

            # 4. Agent templates
            if hasattr(plugin, "get_agents"):
                agents = plugin.get_agents()
                if agents:
                    self._agent_templates[pid] = list(agents)
                    receipt.agent_template_count = len(agents)

        except Exception as exc:
            exc.partial_receipt = receipt  # type: ignore[attr-defined]
            raise

        return receipt

    def rollback(self, receipt: PluginApplyReceipt) -> None:
        """Undo all registrations recorded in receipt."""
        # Reverse order: agents → commands → tools → hooks
        self._agent_templates.pop(receipt.plugin_id, None)

        if self._skill_router is not None:
            for sid in receipt.skill_ids:
                try:
                    self._skill_router.remove_skill(sid)
                except Exception:
                    pass

        if self._tool_registry is not None:
            for tname in receipt.tool_names:
                try:
                    self._tool_registry.remove(tname)
                except Exception:
                    pass

        for hid in receipt.hook_ids:
            self._hooks.unregister(hid)

        logger.info(
            "plugin.extensions_rolled_back",
            plugin_id=receipt.plugin_id,
            hooks=len(receipt.hook_ids),
            tools=len(receipt.tool_names),
            commands=len(receipt.skill_ids),
        )

    def remove(self, plugin: Any) -> None:
        """Remove all extensions for a plugin (used during disable)."""
        pid = plugin.manifest.plugin_id

        # Hooks
        hooks = self._hooks.list_hooks(plugin_id=pid, enabled_only=False)
        for meta in hooks:
            self._hooks.unregister(meta.hook_id)

        # Tools
        if self._tool_registry is not None:
            tools = plugin.get_tools()
            if tools:
                for tool_entry in tools:
                    try:
                        self._tool_registry.remove(tool_entry.meta.name)
                    except Exception:
                        pass

        # Commands
        if self._skill_router is not None and hasattr(plugin, "get_commands"):
            commands = plugin.get_commands()
            if commands:
                for cmd in commands:
                    sid = getattr(cmd, "skill_id", None) or str(cmd)
                    try:
                        self._skill_router.remove_skill(sid)
                    except Exception:
                        pass

        # Agent templates
        self._agent_templates.pop(pid, None)

    def list_agent_templates(self) -> dict[str, list]:
        return dict(self._agent_templates)

    def get_agent_templates(self, plugin_id: str) -> list:
        return list(self._agent_templates.get(plugin_id, []))
