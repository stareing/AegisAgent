"""PluginRegistry — tracks discovered and loaded plugins with OC-compatible records."""

from __future__ import annotations

from datetime import datetime, timezone

from agent_framework.infra.logger import get_logger
from agent_framework.models.plugin import (
    PluginDiagnostic,
    PluginManifest,
    PluginRecord,
    PluginStatus,
)
from agent_framework.plugins.errors import (PluginConflictError,
                                            PluginValidationError)
from agent_framework.plugins.protocol import PluginProtocol

logger = get_logger(__name__)


class _InternalRecord:
    """Internal tracking record for a plugin."""

    __slots__ = ("plugin", "status", "record")

    def __init__(self, plugin: PluginProtocol, status: PluginStatus) -> None:
        self.plugin = plugin
        self.status = status
        manifest = plugin.manifest
        self.record = PluginRecord(
            plugin_id=manifest.plugin_id,
            name=manifest.name,
            version=manifest.version,
            kind=manifest.kind,
            status=status.value,
            enabled=status == PluginStatus.ENABLED,
            channel_ids=list(manifest.channels),
            provider_ids=list(manifest.providers),
            skill_ids=list(manifest.skills),
        )


class PluginRegistry:
    """Central plugin registry — discovery, validation, conflict checking.

    Maintains OC-compatible PluginRecord for each plugin for observability.
    Thread-safety: designed for single-threaded setup phase.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, _InternalRecord] = {}

    def register(self, plugin: PluginProtocol) -> None:
        """Register a plugin. Validates manifest and checks conflicts."""
        manifest = plugin.manifest
        pid = manifest.plugin_id

        if pid in self._plugins:
            raise PluginValidationError(
                f"Duplicate plugin_id: {pid}", plugin_id=pid
            )

        # Check conflicts with already-enabled plugins
        for existing_id, record in self._plugins.items():
            if record.status == PluginStatus.ENABLED:
                if existing_id in manifest.conflicts:
                    raise PluginConflictError(
                        f"Plugin '{pid}' conflicts with enabled plugin '{existing_id}'",
                        plugin_id=pid,
                    )
                if pid in record.plugin.manifest.conflicts:
                    raise PluginConflictError(
                        f"Enabled plugin '{existing_id}' conflicts with '{pid}'",
                        plugin_id=pid,
                    )

        self._plugins[pid] = _InternalRecord(plugin, PluginStatus.DISCOVERED)
        logger.info(
            "plugin.registered",
            plugin_id=pid,
            version=manifest.version,
            kind=manifest.kind.value,
            provides_hooks=manifest.provides_hooks,
            provides_tools=manifest.provides_tools,
        )

    def unregister(self, plugin_id: str) -> None:
        """Remove a plugin from the registry."""
        record = self._plugins.pop(plugin_id, None)
        if record:
            logger.info("plugin.unregistered", plugin_id=plugin_id)

    def get(self, plugin_id: str) -> PluginProtocol | None:
        record = self._plugins.get(plugin_id)
        return record.plugin if record else None

    def get_status(self, plugin_id: str) -> PluginStatus | None:
        record = self._plugins.get(plugin_id)
        return record.status if record else None

    def set_status(self, plugin_id: str, status: PluginStatus) -> None:
        record = self._plugins.get(plugin_id)
        if record:
            record.status = status
            record.record.status = status.value
            record.record.enabled = status == PluginStatus.ENABLED

    def get_record(self, plugin_id: str) -> PluginRecord | None:
        """Return OC-compatible PluginRecord for observability."""
        record = self._plugins.get(plugin_id)
        return record.record if record else None

    def update_record(
        self,
        plugin_id: str,
        *,
        tool_names: list[str] | None = None,
        hook_names: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Update the runtime record after plugin enable/disable."""
        record = self._plugins.get(plugin_id)
        if not record:
            return
        if tool_names is not None:
            record.record.tool_names = tool_names
        if hook_names is not None:
            record.record.hook_names = hook_names
        if error is not None:
            record.record.error = error

    def add_diagnostic(
        self,
        plugin_id: str,
        level: str,
        message: str,
    ) -> None:
        """Add a diagnostic entry to a plugin's record."""
        record = self._plugins.get(plugin_id)
        if not record:
            return
        diag = PluginDiagnostic(
            plugin_id=plugin_id,
            level=level,
            message=message,
            timestamp=datetime.now(timezone.utc),
        )
        record.record.diagnostics.append(diag)

    def list_plugins(
        self,
        status: PluginStatus | None = None,
    ) -> list[PluginManifest]:
        """List plugin manifests, optionally filtered by status."""
        result: list[PluginManifest] = []
        for record in self._plugins.values():
            if status is not None and record.status != status:
                continue
            result.append(record.plugin.manifest)
        return result

    def list_records(
        self,
        status: PluginStatus | None = None,
    ) -> list[PluginRecord]:
        """List OC-compatible PluginRecords for all plugins."""
        result: list[PluginRecord] = []
        for record in self._plugins.values():
            if status is not None and record.status != status:
                continue
            result.append(record.record)
        return result

    def list_enabled(self) -> list[PluginProtocol]:
        """Return all enabled plugins."""
        return [
            r.plugin for r in self._plugins.values()
            if r.status == PluginStatus.ENABLED
        ]

    def check_dependencies(self, plugin_id: str) -> list[str]:
        """Return list of missing dependency plugin IDs."""
        record = self._plugins.get(plugin_id)
        if not record:
            return []
        missing = []
        for dep in record.plugin.manifest.dependencies:
            if dep not in self._plugins:
                missing.append(dep)
        return missing

    def clear(self) -> None:
        self._plugins.clear()

    @property
    def count(self) -> int:
        return len(self._plugins)
