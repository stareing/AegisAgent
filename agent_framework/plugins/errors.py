"""Plugin subsystem errors."""

from __future__ import annotations


class PluginError(Exception):
    """Base error for plugin subsystem."""

    def __init__(self, message: str, plugin_id: str = "") -> None:
        super().__init__(message)
        self.plugin_id = plugin_id


class PluginValidationError(PluginError):
    """Invalid manifest, missing dependencies, version conflict."""


class PluginConflictError(PluginError):
    """Two conflicting plugins cannot be enabled simultaneously."""


class PluginPermissionError(PluginError):
    """Plugin requests permissions not granted."""


class PluginLifecycleError(PluginError):
    """Invalid state transition (e.g. enable from UNLOADED)."""
