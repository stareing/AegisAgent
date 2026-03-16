"""Plugin subsystem — installable, governed extension packages.

A plugin is a distributable unit that can provide:
- Hooks (pre/post tool use, run lifecycle, memory gates, etc.)
- Tool providers
- Agent templates
- Commands

Design invariants:
- Plugins declare capabilities via PluginManifest
- Undeclared permissions are denied
- High-risk permissions require explicit user confirmation
- enable() failure triggers full rollback (no half-registered state)
- Disabled plugins' hooks are never executed
"""

from agent_framework.models.plugin import (
    HIGH_RISK_PERMISSIONS,
    PluginManifest,
    PluginPermission,
    PluginStatus,
)
from agent_framework.plugins.protocol import PluginProtocol
from agent_framework.plugins.registry import PluginRegistry
from agent_framework.plugins.loader import PluginLoader
from agent_framework.plugins.lifecycle import PluginLifecycleManager

__all__ = [
    "HIGH_RISK_PERMISSIONS",
    "PluginManifest",
    "PluginPermission",
    "PluginStatus",
    "PluginProtocol",
    "PluginRegistry",
    "PluginLoader",
    "PluginLifecycleManager",
]
