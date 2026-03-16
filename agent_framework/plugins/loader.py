"""PluginLoader — discovers and loads plugins from directories or entries."""

from __future__ import annotations

import importlib
from pathlib import Path

from agent_framework.infra.logger import get_logger
from agent_framework.models.plugin import PluginManifest, PluginStatus
from agent_framework.plugins.errors import PluginValidationError
from agent_framework.plugins.protocol import PluginProtocol
from agent_framework.plugins.registry import PluginRegistry

logger = get_logger(__name__)


class PluginLoader:
    """Discovers and loads plugins into the registry.

    Supports two discovery methods:
    1. Module-based: import a module that exposes a PluginProtocol
    2. Directory-based: scan directories for plugin packages (future)
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def load_from_module(self, module_path: str) -> PluginManifest:
        """Load a plugin from a Python module path.

        The module must expose a `create_plugin() -> PluginProtocol` factory
        or a module-level `plugin` attribute implementing PluginProtocol.
        """
        try:
            mod = importlib.import_module(module_path)
        except ImportError as e:
            raise PluginValidationError(
                f"Cannot import plugin module: {module_path}: {e}"
            )

        # Try factory first, then attribute
        plugin: PluginProtocol | None = None
        if hasattr(mod, "create_plugin"):
            plugin = mod.create_plugin()
        elif hasattr(mod, "plugin"):
            plugin = mod.plugin
        else:
            raise PluginValidationError(
                f"Module '{module_path}' has no create_plugin() or plugin attribute"
            )

        if not hasattr(plugin, "manifest"):
            raise PluginValidationError(
                f"Plugin from '{module_path}' does not have a manifest"
            )

        self._registry.register(plugin)
        self._registry.set_status(plugin.manifest.plugin_id, PluginStatus.LOADED)

        logger.info(
            "plugin.loaded",
            plugin_id=plugin.manifest.plugin_id,
            module=module_path,
        )
        return plugin.manifest

    def load_plugin(self, plugin: PluginProtocol) -> PluginManifest:
        """Load a plugin instance directly (for programmatic registration)."""
        self._registry.register(plugin)
        plugin.load()
        self._registry.set_status(plugin.manifest.plugin_id, PluginStatus.LOADED)
        logger.info("plugin.loaded", plugin_id=plugin.manifest.plugin_id)
        return plugin.manifest

    def discover_directory(self, plugin_dir: str) -> list[PluginManifest]:
        """Scan a directory for plugin packages (each with plugin.toml or __init__.py).

        Returns manifests of discovered plugins. Does NOT load them.
        """
        discovered: list[PluginManifest] = []
        path = Path(plugin_dir)
        if not path.is_dir():
            logger.warning("plugin.discover_dir_not_found", path=plugin_dir)
            return discovered

        for child in sorted(path.iterdir()):
            if not child.is_dir():
                continue
            init_file = child / "__init__.py"
            if not init_file.exists():
                continue
            # Try to discover manifest from the package
            module_name = child.name
            try:
                manifest = self.load_from_module(f"{plugin_dir}.{module_name}")
                discovered.append(manifest)
            except PluginValidationError as e:
                logger.warning(
                    "plugin.discover_failed",
                    path=str(child),
                    error=str(e),
                )

        return discovered
