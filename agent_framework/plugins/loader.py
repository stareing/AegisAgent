"""PluginLoader — discovers and loads plugins from directories or entries.

Supports three discovery methods:
1. Module-based: import a module that exposes a PluginProtocol
2. Directory-based: scan directories for plugin packages with __init__.py
3. Manifest-based: scan directories for plugin.json files (OC-compatible)
"""

from __future__ import annotations

import importlib
import json
from functools import lru_cache
from pathlib import Path

from agent_framework.infra.logger import get_logger
from agent_framework.models.plugin import PluginManifest, PluginStatus
from agent_framework.plugins.errors import PluginValidationError
from agent_framework.plugins.protocol import PluginProtocol
from agent_framework.plugins.registry import PluginRegistry

logger = get_logger(__name__)

# OC-compatible manifest file names (checked in priority order)
MANIFEST_FILE_NAMES: tuple[str, ...] = ("plugin.json", "plugin.toml")

# LRU cache size for manifest loading
_MANIFEST_CACHE_SIZE = 128


@lru_cache(maxsize=_MANIFEST_CACHE_SIZE)
def _load_manifest_from_json(manifest_path: str) -> PluginManifest:
    """Parse a plugin.json file into a PluginManifest (cached)."""
    path = Path(manifest_path)
    with open(path) as f:
        data = json.load(f)

    # OC-compatibility: map OC field names to framework field names
    if "id" in data and "plugin_id" not in data:
        data["plugin_id"] = data.pop("id")
    if "configSchema" in data and "config_schema" not in data:
        data["config_schema"] = data.pop("configSchema")
    if "enabledByDefault" in data and "enabled_by_default" not in data:
        data["enabled_by_default"] = data.pop("enabledByDefault")
    if "uiHints" in data and "ui_hints" not in data:
        data["ui_hints"] = data.pop("uiHints")
    if "minHostVersion" in data and "min_host_version" not in data:
        data["min_host_version"] = data.pop("minHostVersion")

    # Provide defaults for required fields missing in OC manifests
    data.setdefault("name", data.get("plugin_id", "unknown"))
    data.setdefault("version", "0.0.0")

    return PluginManifest(**data)


class PluginLoader:
    """Discovers and loads plugins into the registry.

    Supports module-based, directory-based, and manifest-based discovery.
    Failed discoveries log warnings but never crash the loader.
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

    def load_from_manifest(self, manifest_path: str | Path) -> PluginManifest:
        """Load plugin metadata from a plugin.json manifest file (OC-compatible).

        This loads ONLY the manifest. The plugin module is loaded separately
        when the entry_module field is set and the plugin is enabled.
        """
        manifest = _load_manifest_from_json(str(manifest_path))
        logger.info(
            "plugin.manifest_discovered",
            plugin_id=manifest.plugin_id,
            path=str(manifest_path),
        )
        return manifest

    def discover_directory(self, plugin_dir: str) -> list[PluginManifest]:
        """Scan a directory for plugin packages.

        Checks each subdirectory for:
        1. plugin.json (OC-compatible manifest — preferred)
        2. __init__.py (Python module-based plugin)

        Returns manifests of discovered plugins.
        """
        discovered: list[PluginManifest] = []
        path = Path(plugin_dir)
        if not path.is_dir():
            logger.warning("plugin.discover_dir_not_found", path=plugin_dir)
            return discovered

        for child in sorted(path.iterdir()):
            if not child.is_dir():
                continue

            # Try manifest-based discovery first (OC-compatible)
            manifest_found = False
            for manifest_name in MANIFEST_FILE_NAMES:
                manifest_file = child / manifest_name
                if manifest_file.exists() and manifest_name.endswith(".json"):
                    try:
                        manifest = self.load_from_manifest(manifest_file)
                        discovered.append(manifest)
                        manifest_found = True
                        break
                    except Exception as e:
                        logger.warning(
                            "plugin.manifest_parse_failed",
                            path=str(manifest_file),
                            error=str(e),
                        )

            if manifest_found:
                continue

            # Fallback: Python module-based discovery
            init_file = child / "__init__.py"
            if not init_file.exists():
                continue
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

    @staticmethod
    def clear_manifest_cache() -> None:
        """Clear the LRU cache for manifest loading (useful in tests)."""
        _load_manifest_from_json.cache_clear()
