"""Plugin data models — manifest, lifecycle, and permission models.

All plugin-related pydantic models live here (under models/).
The plugins/ package re-exports from this module.

OC-compatible manifest schema supports cross-ecosystem plugin discovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    REGISTER_CHANNELS = "register_channels"
    REGISTER_PROVIDERS = "register_providers"
    MODIFY_CONTEXT = "modify_context"
    ACCESS_NETWORK = "access_network"


# High-risk permissions that need explicit user confirmation
HIGH_RISK_PERMISSIONS: frozenset[PluginPermission] = frozenset({
    PluginPermission.REGISTER_TOOLS,
    PluginPermission.SPAWN_AGENT,
    PluginPermission.REGISTER_CHANNELS,
    PluginPermission.REGISTER_PROVIDERS,
    PluginPermission.ACCESS_NETWORK,
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
# Plugin Kind — classifies plugin type (OC-compatible)
# ---------------------------------------------------------------------------

class PluginKind(str, Enum):
    """Plugin type classification for registry filtering and UI display."""

    GENERAL = "general"
    MEMORY = "memory"
    CONTEXT_ENGINE = "context_engine"
    PROVIDER = "provider"
    CHANNEL = "channel"
    TOOL = "tool"


# ---------------------------------------------------------------------------
# Plugin Config UI Hints (OC-compatible)
# ---------------------------------------------------------------------------

class PluginConfigUiHint(BaseModel):
    """UI rendering hints for plugin configuration fields."""

    model_config = {"frozen": True}

    label: str = ""
    help: str = ""
    tags: list[str] = Field(default_factory=list)
    advanced: bool = False
    sensitive: bool = False


# ---------------------------------------------------------------------------
# Plugin Diagnostic — error/warning tracking per plugin
# ---------------------------------------------------------------------------

class PluginDiagnostic(BaseModel):
    """Diagnostic entry for plugin errors and warnings."""

    model_config = {"frozen": True}

    plugin_id: str
    level: str = "error"  # "error" | "warning" | "info"
    message: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Plugin Record — runtime status tracking (OC-compatible)
# ---------------------------------------------------------------------------

class PluginRecord(BaseModel):
    """Runtime tracking record for a loaded plugin.

    Mirrors OC's PluginRecord for cross-ecosystem observability.
    """

    plugin_id: str
    name: str = ""
    version: str = ""
    kind: PluginKind = PluginKind.GENERAL
    status: str = "discovered"  # loaded | disabled | error | enabled
    enabled: bool = False
    tool_names: list[str] = Field(default_factory=list)
    hook_names: list[str] = Field(default_factory=list)
    provider_ids: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    diagnostics: list[PluginDiagnostic] = Field(default_factory=list)
    error: str | None = None


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

    # OC-compatible extensions (maximize cross-ecosystem interop)
    kind: PluginKind = PluginKind.GENERAL
    channels: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    ui_hints: dict[str, PluginConfigUiHint] | None = None
    min_host_version: str | None = None
