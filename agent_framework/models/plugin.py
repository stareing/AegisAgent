"""Plugin data models — manifest, lifecycle, and permission models.

All plugin-related pydantic models live here (under models/).
The plugins/ package re-exports from this module.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Plugin Permission
# ---------------------------------------------------------------------------

class PluginPermission(str, Enum):
    """Declarative permission system for plugins.

    Default: minimum privilege — undeclared means denied.
    High-risk permissions require explicit user confirmation.
    """

    READ_RUN_METADATA = "read_run_metadata"
    READ_SESSION_SNAPSHOT = "read_session_snapshot"
    READ_TOOL_REQUEST = "read_tool_request"
    READ_TOOL_RESULT = "read_tool_result"
    READ_MEMORY_CANDIDATE = "read_memory_candidate"
    EMIT_NOTIFICATION = "emit_notification"
    EMIT_ARTIFACT = "emit_artifact"
    REGISTER_TOOLS = "register_tools"
    REGISTER_HOOKS = "register_hooks"
    SPAWN_AGENT = "spawn_agent"


# High-risk permissions that need explicit user confirmation
HIGH_RISK_PERMISSIONS: frozenset[PluginPermission] = frozenset({
    PluginPermission.REGISTER_TOOLS,
    PluginPermission.SPAWN_AGENT,
})


# ---------------------------------------------------------------------------
# Plugin Status
# ---------------------------------------------------------------------------

class PluginStatus(str, Enum):
    """Plugin lifecycle states."""

    DISCOVERED = "discovered"
    VALIDATED = "validated"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"
    UNLOADED = "unloaded"


# ---------------------------------------------------------------------------
# Plugin Manifest
# ---------------------------------------------------------------------------

class PluginManifest(BaseModel):
    """Declarative metadata for a plugin package.

    Every plugin must provide a manifest. The loader validates it
    before loading the plugin module.
    """

    model_config = {"frozen": True}

    plugin_id: str
    name: str
    version: str
    framework_version_range: str = ">=0.1.0"
    description: str = ""
    author: str | None = None
    homepage: str | None = None

    entry_module: str = ""
    enabled_by_default: bool = False

    provides_hooks: bool = False
    provides_tools: bool = False
    provides_agents: bool = False
    provides_commands: bool = False

    required_permissions: list[PluginPermission] = Field(default_factory=list)
    optional_permissions: list[PluginPermission] = Field(default_factory=list)

    dependencies: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    config_schema: dict | None = None
    marketplace_meta: dict | None = None
