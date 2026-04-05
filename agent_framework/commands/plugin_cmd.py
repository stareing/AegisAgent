"""Hierarchical /plugins slash command for plugin management.

Subcommands:
  list    - List all plugins with enabled/disabled status
  enable  - Enable a plugin by ID
  disable - Disable a plugin by ID
  info    - Show plugin manifest details
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.commands.protocol import (
    CommandActionReturn,
    CommandContext,
    MessageAction,
    SlashCommand,
)

if TYPE_CHECKING:
    from agent_framework.entry import AgentFramework


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_framework(ctx: CommandContext) -> AgentFramework:
    """Extract the framework instance from command context."""
    fw = ctx.framework
    if fw is None:
        raise ValueError("Framework not available in command context")
    return fw


def _get_registry(ctx: CommandContext):
    """Return the plugin registry, or None if not initialized."""
    fw = _get_framework(ctx)
    return getattr(fw, "_plugin_registry_obj", None)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _handle_list(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """List all plugins with their enabled/disabled status."""
    registry = _get_registry(ctx)
    if registry is None:
        return MessageAction(content="Plugin system not initialized.")

    manifests = registry.list_plugins()
    if not manifests:
        return MessageAction(content="No plugins registered.")

    # Build a lookup of enabled plugin IDs for status display
    enabled_ids: set[str] = set()
    try:
        for plugin in registry.list_enabled():
            manifest = getattr(plugin, "manifest", None)
            if manifest:
                enabled_ids.add(manifest.plugin_id)
    except Exception:
        pass

    lines: list[str] = ["Registered plugins:", ""]
    for manifest in manifests:
        status = "enabled" if manifest.plugin_id in enabled_ids else "disabled"
        lines.append(f"  {manifest.plugin_id}  v{manifest.version}  [{status}]")
        if manifest.description:
            lines.append(f"    {manifest.description}")

    lines.append("")
    lines.append(f"Total: {len(manifests)} plugins ({len(enabled_ids)} enabled)")
    return MessageAction(content="\n".join(lines))


def _handle_enable(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Enable a plugin by ID."""
    plugin_id = args.strip()
    if not plugin_id:
        return MessageAction(
            message_type="error",
            content="Usage: /plugins enable <plugin_id>",
        )

    fw = _get_framework(ctx)
    try:
        fw.enable_plugin(plugin_id)
    except Exception as exc:
        return MessageAction(
            message_type="error",
            content=f"Failed to enable plugin '{plugin_id}': {exc}",
        )
    return MessageAction(content=f"Plugin '{plugin_id}' enabled.")


def _handle_disable(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Disable a plugin by ID."""
    plugin_id = args.strip()
    if not plugin_id:
        return MessageAction(
            message_type="error",
            content="Usage: /plugins disable <plugin_id>",
        )

    fw = _get_framework(ctx)
    try:
        fw.disable_plugin(plugin_id)
    except Exception as exc:
        return MessageAction(
            message_type="error",
            content=f"Failed to disable plugin '{plugin_id}': {exc}",
        )
    return MessageAction(content=f"Plugin '{plugin_id}' disabled.")


def _handle_info(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Show detailed info for a specific plugin (manifest fields)."""
    plugin_id = args.strip()
    if not plugin_id:
        return MessageAction(
            message_type="error",
            content="Usage: /plugins info <plugin_id>",
        )

    registry = _get_registry(ctx)
    if registry is None:
        return MessageAction(
            message_type="error",
            content="Plugin system not initialized.",
        )

    # Find the manifest matching the requested ID
    manifest = None
    for m in registry.list_plugins():
        if m.plugin_id == plugin_id:
            manifest = m
            break

    if manifest is None:
        return MessageAction(
            message_type="error",
            content=f"Plugin '{plugin_id}' not found.",
        )

    # Check enabled status
    enabled_ids: set[str] = set()
    try:
        for plugin in registry.list_enabled():
            pm = getattr(plugin, "manifest", None)
            if pm:
                enabled_ids.add(pm.plugin_id)
    except Exception:
        pass
    status = "enabled" if plugin_id in enabled_ids else "disabled"

    lines: list[str] = [
        f"Plugin: {manifest.name} ({manifest.plugin_id})",
        f"  Version:     {manifest.version}",
        f"  Status:      {status}",
        f"  Description: {manifest.description or '(none)'}",
        f"  Author:      {manifest.author or '(unknown)'}",
        f"  Homepage:    {manifest.homepage or '(none)'}",
        f"  Kind:        {manifest.kind.value if hasattr(manifest.kind, 'value') else manifest.kind}",
        "",
        "  Capabilities:",
        f"    Hooks:    {manifest.provides_hooks}",
        f"    Tools:    {manifest.provides_tools}",
        f"    Agents:   {manifest.provides_agents}",
        f"    Commands: {manifest.provides_commands}",
    ]

    if manifest.dependencies:
        lines.append(f"  Dependencies: {', '.join(manifest.dependencies)}")
    if manifest.conflicts:
        lines.append(f"  Conflicts:    {', '.join(manifest.conflicts)}")
    if manifest.required_permissions:
        perm_names = [p.value if hasattr(p, "value") else str(p) for p in manifest.required_permissions]
        lines.append(f"  Permissions:  {', '.join(perm_names)}")

    return MessageAction(content="\n".join(lines))


def _handle_plugins_root(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Default handler when no subcommand given — show usage."""
    return MessageAction(
        content=(
            "Usage: /plugins <subcommand>\n"
            "\n"
            "Subcommands:\n"
            "  list    - List all plugins with status\n"
            "  enable  - Enable a plugin: /plugins enable <id>\n"
            "  disable - Disable a plugin: /plugins disable <id>\n"
            "  info    - Show plugin details: /plugins info <id>"
        ),
    )


# ---------------------------------------------------------------------------
# Exported SlashCommand instance
# ---------------------------------------------------------------------------

plugins_command = SlashCommand(
    name="plugins",
    description="Manage plugins (list, enable, disable, info)",
    aliases=["plugin"],
    category="plugins",
    handler=_handle_plugins_root,
    subcommands=[
        SlashCommand(
            name="list",
            description="List all plugins with enabled/disabled status",
            aliases=["ls"],
            handler=_handle_list,
        ),
        SlashCommand(
            name="enable",
            description="Enable a plugin by ID",
            handler=_handle_enable,
        ),
        SlashCommand(
            name="disable",
            description="Disable a plugin by ID",
            handler=_handle_disable,
        ),
        SlashCommand(
            name="info",
            description="Show detailed plugin manifest info",
            handler=_handle_info,
        ),
    ],
)
