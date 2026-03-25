"""PluginLifecycleManager — orchestrates plugin enable/disable/unload with rollback.

Invariants:
- enable() failure must leave no half-registered state
- disabled plugin's hooks are never executed
- unloaded plugin has no active references
"""

from __future__ import annotations

from typing import Any

from agent_framework.hooks.registry import HookRegistry
from agent_framework.infra.logger import get_logger
from agent_framework.models.plugin import (HIGH_RISK_PERMISSIONS,
                                           PluginPermission, PluginStatus)
from agent_framework.plugins.errors import (PluginLifecycleError,
                                            PluginPermissionError,
                                            PluginValidationError)
from agent_framework.plugins.extensions import (PluginApplyReceipt,
                                                PluginExtensionRegistrar)
from agent_framework.plugins.protocol import PluginProtocol
from agent_framework.plugins.registry import PluginRegistry

logger = get_logger(__name__)


class PluginLifecycleManager:
    """Manages plugin state transitions with safety guarantees.

    Lifecycle: DISCOVERED → VALIDATED → LOADED → ENABLED ⇄ DISABLED → UNLOADED
    """

    def __init__(
        self,
        plugin_registry: PluginRegistry,
        hook_registry: HookRegistry,
        granted_permissions: set[PluginPermission] | None = None,
        tool_registry: Any = None,
        skill_router: Any = None,
    ) -> None:
        self._plugins = plugin_registry
        self._hooks = hook_registry
        self._granted_permissions = granted_permissions or set()
        self._registrar = PluginExtensionRegistrar(
            hook_registry=hook_registry,
            tool_registry=tool_registry,
            skill_router=skill_router,
        )

    def validate(self, plugin_id: str) -> None:
        """Validate manifest, check dependencies and permissions."""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginLifecycleError(
                f"Plugin '{plugin_id}' not found", plugin_id=plugin_id
            )

        manifest = plugin.manifest

        # Check dependencies
        missing = self._plugins.check_dependencies(plugin_id)
        if missing:
            raise PluginValidationError(
                f"Plugin '{plugin_id}' missing dependencies: {missing}",
                plugin_id=plugin_id,
            )

        # Check required permissions
        for perm in manifest.required_permissions:
            if perm not in self._granted_permissions:
                if perm in HIGH_RISK_PERMISSIONS:
                    raise PluginPermissionError(
                        f"Plugin '{plugin_id}' requires high-risk permission "
                        f"'{perm.value}' which is not granted",
                        plugin_id=plugin_id,
                    )
                raise PluginPermissionError(
                    f"Plugin '{plugin_id}' requires permission "
                    f"'{perm.value}' which is not granted",
                    plugin_id=plugin_id,
                )

        # Check framework version compatibility
        try:
            from agent_framework import __version__ as fw_version
        except ImportError:
            fw_version = "0.1.0"
        if not self._check_version_range(manifest.framework_version_range, fw_version):
            raise PluginValidationError(
                f"Plugin '{plugin_id}' requires framework {manifest.framework_version_range}, "
                f"but current is {fw_version}",
                plugin_id=plugin_id,
            )

        self._plugins.set_status(plugin_id, PluginStatus.VALIDATED)
        logger.info("plugin.validated", plugin_id=plugin_id)

    def enable(self, plugin_id: str) -> None:
        """Enable a plugin — validate first, then register its hooks and tools.

        Validation (dependencies, permissions, version) is mandatory before
        any registrations. On failure, all registrations are rolled back.
        """
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginLifecycleError(
                f"Plugin '{plugin_id}' not found", plugin_id=plugin_id
            )

        status = self._plugins.get_status(plugin_id)
        if status not in (PluginStatus.LOADED, PluginStatus.DISABLED, PluginStatus.VALIDATED):
            raise PluginLifecycleError(
                f"Cannot enable plugin '{plugin_id}' from status '{status}'",
                plugin_id=plugin_id,
            )

        # Mandatory validation gate — cannot be bypassed
        if status != PluginStatus.VALIDATED:
            self.validate(plugin_id)

        receipt = PluginApplyReceipt(plugin_id=plugin_id)
        try:
            plugin.enable()
            receipt = self._registrar.apply(plugin)

            self._plugins.set_status(plugin_id, PluginStatus.ENABLED)
            # Update OC-compatible PluginRecord with registration details
            self._plugins.update_record(
                plugin_id,
                tool_names=receipt.tool_names,
                hook_names=receipt.hook_ids,
            )
            logger.info(
                "plugin.enabled",
                plugin_id=plugin_id,
                hooks_registered=len(receipt.hook_ids),
                tools_registered=len(receipt.tool_names),
                commands_registered=len(receipt.skill_ids),
                agents_registered=receipt.agent_template_count,
            )

        except Exception as e:
            partial = getattr(e, "partial_receipt", receipt)
            self._registrar.rollback(partial)
            self._plugins.set_status(plugin_id, PluginStatus.FAILED)
            # Track error in PluginRecord for observability
            error_msg = str(e)
            self._plugins.update_record(plugin_id, error=error_msg)
            self._plugins.add_diagnostic(plugin_id, "error", f"enable failed: {error_msg}")
            logger.error(
                "plugin.enable_failed",
                plugin_id=plugin_id,
                error=error_msg,
            )
            raise PluginLifecycleError(
                f"Failed to enable plugin '{plugin_id}': {e}",
                plugin_id=plugin_id,
            ) from e

    def disable(self, plugin_id: str) -> None:
        """Disable a plugin — unregister all its hooks."""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginLifecycleError(
                f"Plugin '{plugin_id}' not found", plugin_id=plugin_id
            )

        status = self._plugins.get_status(plugin_id)
        if status != PluginStatus.ENABLED:
            raise PluginLifecycleError(
                f"Cannot disable plugin '{plugin_id}' from status '{status}'",
                plugin_id=plugin_id,
            )

        self._registrar.remove(plugin)

        plugin.disable()
        self._plugins.set_status(plugin_id, PluginStatus.DISABLED)
        logger.info("plugin.disabled", plugin_id=plugin_id)

    def unload(self, plugin_id: str) -> None:
        """Unload a plugin — disable first if needed, then release."""
        status = self._plugins.get_status(plugin_id)
        if status == PluginStatus.ENABLED:
            self.disable(plugin_id)

        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return

        plugin.unload()
        self._plugins.set_status(plugin_id, PluginStatus.UNLOADED)
        logger.info("plugin.unloaded", plugin_id=plugin_id)

    @staticmethod
    def _check_version_range(version_range: str, current_version: str) -> bool:
        """Simple version range check. Supports >=X.Y.Z, <=X.Y.Z, ==X.Y.Z formats."""
        if not version_range or version_range == "*":
            return True
        try:
            from packaging.version import Version
            current = Version(current_version)
            if version_range.startswith(">="):
                return current >= Version(version_range[2:])
            if version_range.startswith("<="):
                return current <= Version(version_range[2:])
            if version_range.startswith("=="):
                return current == Version(version_range[2:])
            return True  # Unknown format, pass
        except Exception:
            return True  # If packaging not installed or parse fails, pass

    def list_agent_templates(self) -> dict[str, list]:
        return self._registrar.list_agent_templates()

    def get_agent_templates(self, plugin_id: str) -> list:
        return self._registrar.get_agent_templates(plugin_id)

    def grant_permission(self, permission: PluginPermission) -> None:
        """Grant a permission for plugin validation."""
        self._granted_permissions.add(permission)

    def revoke_permission(self, permission: PluginPermission) -> None:
        """Revoke a previously granted permission."""
        self._granted_permissions.discard(permission)
