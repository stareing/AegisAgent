"""Tests for the plugins subsystem.

Covers:
1. Plugin models (PluginManifest, PluginPermission, PluginStatus)
2. PluginRegistry (register/conflict/dependency checking)
3. PluginLoader (module loading, direct loading)
4. PluginLifecycleManager (enable/disable/unload with rollback)
5. Architecture guards (permission enforcement, conflict detection)
"""

from __future__ import annotations

import pytest

from agent_framework.models.plugin import (
    HIGH_RISK_PERMISSIONS,
    PluginManifest,
    PluginPermission,
    PluginStatus,
)
from agent_framework.models.hook import (
    HookContext,
    HookMeta,
    HookPoint,
    HookResult,
    HookResultAction,
)
from agent_framework.hooks.registry import HookRegistry
from agent_framework.plugins.registry import PluginRegistry
from agent_framework.plugins.lifecycle import PluginLifecycleManager
from agent_framework.plugins.loader import PluginLoader
from agent_framework.plugins.errors import (
    PluginConflictError,
    PluginLifecycleError,
    PluginPermissionError,
    PluginValidationError,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _SimpleHook:
    def __init__(self, hook_id: str = "test_hook", plugin_id: str = "test_plugin") -> None:
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id=plugin_id,
            hook_point=HookPoint.RUN_START,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        return HookResult(action=HookResultAction.NOOP)


class _TestPlugin:
    """Minimal plugin for testing."""

    def __init__(
        self,
        plugin_id: str = "test_plugin",
        hooks: list | None = None,
        conflicts: list[str] | None = None,
        dependencies: list[str] | None = None,
        required_permissions: list[PluginPermission] | None = None,
    ) -> None:
        self._manifest = PluginManifest(
            plugin_id=plugin_id,
            name=f"Test Plugin {plugin_id}",
            version="1.0.0",
            provides_hooks=True,
            conflicts=conflicts or [],
            dependencies=dependencies or [],
            required_permissions=required_permissions or [],
        )
        self._hooks = hooks or [_SimpleHook(f"{plugin_id}.hook", plugin_id=plugin_id)]
        self._loaded = False
        self._enabled = False

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def load(self) -> None:
        self._loaded = True

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def unload(self) -> None:
        self._loaded = False

    def get_hooks(self) -> list:
        return self._hooks

    def get_tools(self) -> list:
        return []

    def get_commands(self) -> list:
        return []

    def get_agents(self) -> list:
        return []


class _FailingPlugin(_TestPlugin):
    """Plugin that fails during enable."""

    def enable(self) -> None:
        raise RuntimeError("Enable failed")


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestPluginModels:
    def test_manifest_frozen(self) -> None:
        m = PluginManifest(
            plugin_id="x", name="X", version="1.0.0"
        )
        with pytest.raises(Exception):
            m.plugin_id = "y"  # type: ignore[misc]

    def test_permission_values(self) -> None:
        assert PluginPermission.READ_RUN_METADATA.value == "read_run_metadata"
        assert PluginPermission.REGISTER_TOOLS in HIGH_RISK_PERMISSIONS
        assert PluginPermission.SPAWN_AGENT in HIGH_RISK_PERMISSIONS

    def test_status_values(self) -> None:
        assert PluginStatus.DISCOVERED.value == "discovered"
        assert PluginStatus.ENABLED.value == "enabled"


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestPluginRegistry:
    def test_register_and_list(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin())
        assert reg.count == 1
        manifests = reg.list_plugins()
        assert len(manifests) == 1
        assert manifests[0].plugin_id == "test_plugin"

    def test_duplicate_raises(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1"))
        with pytest.raises(PluginValidationError, match="Duplicate"):
            reg.register(_TestPlugin("p1"))

    def test_conflict_detection(self) -> None:
        reg = PluginRegistry()
        p1 = _TestPlugin("p1")
        reg.register(p1)
        reg.set_status("p1", PluginStatus.ENABLED)

        p2 = _TestPlugin("p2", conflicts=["p1"])
        with pytest.raises(PluginConflictError):
            reg.register(p2)

    def test_bidirectional_conflict(self) -> None:
        reg = PluginRegistry()
        p1 = _TestPlugin("p1", conflicts=["p2"])
        reg.register(p1)
        reg.set_status("p1", PluginStatus.ENABLED)

        with pytest.raises(PluginConflictError):
            reg.register(_TestPlugin("p2"))

    def test_check_dependencies_missing(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1", dependencies=["missing_dep"]))
        missing = reg.check_dependencies("p1")
        assert missing == ["missing_dep"]

    def test_check_dependencies_satisfied(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("dep"))
        reg.register(_TestPlugin("p1", dependencies=["dep"]))
        missing = reg.check_dependencies("p1")
        assert missing == []

    def test_list_enabled(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1"))
        reg.register(_TestPlugin("p2"))
        reg.set_status("p1", PluginStatus.ENABLED)
        enabled = reg.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].manifest.plugin_id == "p1"

    def test_filter_by_status(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1"))
        reg.register(_TestPlugin("p2"))
        reg.set_status("p1", PluginStatus.ENABLED)
        manifests = reg.list_plugins(status=PluginStatus.ENABLED)
        assert len(manifests) == 1

    def test_unregister(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1"))
        reg.unregister("p1")
        assert reg.count == 0

    def test_clear(self) -> None:
        reg = PluginRegistry()
        reg.register(_TestPlugin("p1"))
        reg.register(_TestPlugin("p2"))
        reg.clear()
        assert reg.count == 0


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

class TestPluginLifecycleManager:
    def _make_lifecycle(
        self, granted: set[PluginPermission] | None = None
    ) -> tuple[PluginRegistry, HookRegistry, PluginLifecycleManager]:
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg, granted or set())
        return preg, hreg, lm

    def test_validate_success(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        preg.register(_TestPlugin("p1"))
        preg.set_status("p1", PluginStatus.LOADED)
        lm.validate("p1")
        assert preg.get_status("p1") == PluginStatus.VALIDATED

    def test_validate_missing_dependency(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        preg.register(_TestPlugin("p1", dependencies=["missing"]))
        with pytest.raises(PluginValidationError, match="missing"):
            lm.validate("p1")

    def test_validate_missing_permission(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        preg.register(_TestPlugin(
            "p1",
            required_permissions=[PluginPermission.REGISTER_TOOLS],
        ))
        with pytest.raises(PluginPermissionError, match="high-risk"):
            lm.validate("p1")

    def test_validate_with_granted_permission(self) -> None:
        preg, hreg, lm = self._make_lifecycle(
            granted={PluginPermission.REGISTER_TOOLS}
        )
        preg.register(_TestPlugin(
            "p1",
            required_permissions=[PluginPermission.REGISTER_TOOLS],
        ))
        lm.validate("p1")  # Should not raise

    def test_enable_registers_hooks(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        plugin = _TestPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        lm.enable("p1")
        assert preg.get_status("p1") == PluginStatus.ENABLED
        assert hreg.count == 1

    def test_enable_failure_rolls_back(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        plugin = _FailingPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)

        with pytest.raises(PluginLifecycleError, match="Enable failed"):
            lm.enable("p1")

        assert preg.get_status("p1") == PluginStatus.FAILED
        assert hreg.count == 0  # Hooks rolled back

    def test_disable_unregisters_hooks(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        plugin = _TestPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        lm.enable("p1")
        assert hreg.count == 1

        lm.disable("p1")
        assert preg.get_status("p1") == PluginStatus.DISABLED
        assert hreg.count == 0

    def test_disable_non_enabled_raises(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        preg.register(_TestPlugin("p1"))
        preg.set_status("p1", PluginStatus.LOADED)
        with pytest.raises(PluginLifecycleError, match="Cannot disable"):
            lm.disable("p1")

    def test_unload_disables_then_unloads(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        plugin = _TestPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        lm.enable("p1")
        assert hreg.count == 1

        lm.unload("p1")
        assert preg.get_status("p1") == PluginStatus.UNLOADED
        assert hreg.count == 0

    def test_enable_from_disabled(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        plugin = _TestPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        lm.enable("p1")
        lm.disable("p1")
        # Re-enable should work
        lm.enable("p1")
        assert preg.get_status("p1") == PluginStatus.ENABLED

    def test_enable_auto_validates(self) -> None:
        """enable() must call validate() — missing permissions block enable."""
        preg, hreg, lm = self._make_lifecycle()  # No permissions granted
        plugin = _TestPlugin(
            "p1",
            required_permissions=[PluginPermission.REGISTER_TOOLS],
        )
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        with pytest.raises(PluginPermissionError, match="high-risk"):
            lm.enable("p1")

    def test_enable_auto_validates_dependencies(self) -> None:
        """enable() must check dependencies even without explicit validate()."""
        preg, hreg, lm = self._make_lifecycle()
        plugin = _TestPlugin("p1", dependencies=["missing_dep"])
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        with pytest.raises(PluginValidationError, match="missing"):
            lm.enable("p1")

    def test_grant_revoke_permission(self) -> None:
        preg, hreg, lm = self._make_lifecycle()
        lm.grant_permission(PluginPermission.REGISTER_TOOLS)
        preg.register(_TestPlugin(
            "p1",
            required_permissions=[PluginPermission.REGISTER_TOOLS],
        ))
        lm.validate("p1")  # Should pass

        lm.revoke_permission(PluginPermission.REGISTER_TOOLS)
        preg.register(_TestPlugin(
            "p2",
            required_permissions=[PluginPermission.REGISTER_TOOLS],
        ))
        with pytest.raises(PluginPermissionError):
            lm.validate("p2")


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------

class TestPluginLoader:
    def test_load_plugin_directly(self) -> None:
        reg = PluginRegistry()
        loader = PluginLoader(reg)
        plugin = _TestPlugin("direct_load")
        manifest = loader.load_plugin(plugin)
        assert manifest.plugin_id == "direct_load"
        assert reg.get_status("direct_load") == PluginStatus.LOADED

    def test_load_nonexistent_module_raises(self) -> None:
        reg = PluginRegistry()
        loader = PluginLoader(reg)
        with pytest.raises(PluginValidationError, match="Cannot import"):
            loader.load_from_module("nonexistent.module.path")


# ---------------------------------------------------------------------------
# Architecture guard tests
# ---------------------------------------------------------------------------

class TestPluginArchitectureGuards:
    def test_disabled_plugin_hooks_not_in_chain(self) -> None:
        """After disabling, plugin's hooks must not be in any resolve chain."""
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg)

        plugin = _TestPlugin("p1")
        preg.register(plugin)
        preg.set_status("p1", PluginStatus.LOADED)
        lm.enable("p1")
        assert len(hreg.resolve_chain(HookPoint.RUN_START)) == 1

        lm.disable("p1")
        assert len(hreg.resolve_chain(HookPoint.RUN_START)) == 0

    def test_conflicting_plugins_cannot_both_enable(self) -> None:
        """Two conflicting plugins cannot be registered simultaneously."""
        reg = PluginRegistry()
        p1 = _TestPlugin("p1", conflicts=["p2"])
        reg.register(p1)
        reg.set_status("p1", PluginStatus.ENABLED)

        with pytest.raises(PluginConflictError):
            reg.register(_TestPlugin("p2"))

    def test_manifest_is_immutable(self) -> None:
        m = PluginManifest(plugin_id="x", name="X", version="1.0.0")
        with pytest.raises(Exception):
            m.name = "Y"  # type: ignore[misc]

    def test_high_risk_permissions_require_explicit_grant(self) -> None:
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg)  # No permissions granted

        for perm in HIGH_RISK_PERMISSIONS:
            pid = f"plugin_{perm.value}"
            preg.register(_TestPlugin(pid, required_permissions=[perm]))
            with pytest.raises(PluginPermissionError, match="high-risk"):
                lm.validate(pid)


# ---------------------------------------------------------------------------
# Plugin tool registration tests
# ---------------------------------------------------------------------------

class TestPluginToolRegistration:
    """Verify that plugin enable/disable also registers/unregisters tools."""

    def test_enable_registers_tools(self) -> None:
        """Plugin tools should be registered in tool_registry on enable."""
        from unittest.mock import MagicMock
        preg = PluginRegistry()
        hreg = HookRegistry()
        mock_tool_reg = MagicMock()

        lm = PluginLifecycleManager(preg, hreg, tool_registry=mock_tool_reg)

        # Create plugin with a mock tool
        from agent_framework.models.tool import ToolEntry, ToolMeta
        tool_entry = ToolEntry(
            meta=ToolMeta(name="plugin_tool", description="test tool"),
        )

        class ToolPlugin(_TestPlugin):
            def get_tools(self):
                return [tool_entry]

        plugin = ToolPlugin("tool_plugin")
        preg.register(plugin)
        preg.set_status("tool_plugin", PluginStatus.LOADED)
        lm.enable("tool_plugin")

        mock_tool_reg.register.assert_called_once_with(tool_entry)

    def test_enable_failure_rolls_back_tools(self) -> None:
        """If enable fails after tools registered, tools should be unregistered."""
        from unittest.mock import MagicMock
        preg = PluginRegistry()
        hreg = HookRegistry()
        # Mock register to succeed, but have a second registration fail
        mock_tool_reg = MagicMock()
        # Tools register fine, but then the tool_registry.register raises
        # on the second tool to simulate mid-registration failure
        call_count = [0]
        def side_effect(entry):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("Registry full")
        mock_tool_reg.register.side_effect = side_effect

        lm = PluginLifecycleManager(preg, hreg, tool_registry=mock_tool_reg)

        from agent_framework.models.tool import ToolEntry, ToolMeta
        tool1 = ToolEntry(meta=ToolMeta(name="tool_ok", description="ok"))
        tool2 = ToolEntry(meta=ToolMeta(name="tool_fail", description="fail"))

        class TwoToolPlugin(_TestPlugin):
            def get_tools(self):
                return [tool1, tool2]

        plugin = TwoToolPlugin("fail_plugin")
        preg.register(plugin)
        preg.set_status("fail_plugin", PluginStatus.LOADED)

        with pytest.raises(PluginLifecycleError):
            lm.enable("fail_plugin")

        # First tool should have been rolled back via remove() (ToolRegistry API)
        mock_tool_reg.remove.assert_called_once_with("tool_ok")


class TestPluginCommandLifecycle:
    """Verify commands (skills) are properly registered and unregistered."""

    def test_disable_removes_commands(self) -> None:
        """After disable, plugin commands must not remain in SkillRouter."""
        from unittest.mock import MagicMock
        from agent_framework.models.agent import Skill

        preg = PluginRegistry()
        hreg = HookRegistry()
        mock_router = MagicMock()
        lm = PluginLifecycleManager(preg, hreg, skill_router=mock_router)

        test_skill = Skill(skill_id="plugin_cmd", name="Plugin Command",
                           description="test")

        class CmdPlugin(_TestPlugin):
            def get_commands(self):
                return [test_skill]

        plugin = CmdPlugin("cmd_plugin")
        preg.register(plugin)
        preg.set_status("cmd_plugin", PluginStatus.LOADED)
        lm.enable("cmd_plugin")

        mock_router.register_skill.assert_called_once_with(test_skill)

        lm.disable("cmd_plugin")
        mock_router.remove_skill.assert_called_once_with("plugin_cmd")

    def test_enable_rollback_removes_commands(self) -> None:
        """If enable fails after commands registered, commands must be rolled back."""
        from unittest.mock import MagicMock
        from agent_framework.models.agent import Skill

        preg = PluginRegistry()
        hreg = HookRegistry()
        mock_router = MagicMock()
        lm = PluginLifecycleManager(preg, hreg, skill_router=mock_router)

        test_skill = Skill(skill_id="will_rollback", name="RB",
                           description="test")

        class CmdFailPlugin(_TestPlugin):
            """Plugin where get_commands succeeds but get_agents raises."""
            def get_commands(self):
                return [test_skill]
            def get_agents(self):
                raise RuntimeError("agent registration boom")

        plugin = CmdFailPlugin("fail_cmd_plugin")
        preg.register(plugin)
        preg.set_status("fail_cmd_plugin", PluginStatus.LOADED)

        with pytest.raises(PluginLifecycleError):
            lm.enable("fail_cmd_plugin")

        # Command should have been rolled back
        mock_router.remove_skill.assert_called_once_with("will_rollback")


class TestPluginAgentTemplates:
    """Verify agent templates have public query API."""

    def test_agent_templates_stored_and_queryable(self) -> None:
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg)

        agent_def = {"agent_id": "reviewer", "model_name": "gpt-4"}

        class AgentPlugin(_TestPlugin):
            def get_agents(self):
                return [agent_def]

        plugin = AgentPlugin("agent_plugin")
        preg.register(plugin)
        preg.set_status("agent_plugin", PluginStatus.LOADED)
        lm.enable("agent_plugin")

        # Public API: query templates
        all_templates = lm.list_agent_templates()
        assert "agent_plugin" in all_templates
        assert all_templates["agent_plugin"] == [agent_def]

        specific = lm.get_agent_templates("agent_plugin")
        assert specific == [agent_def]

    def test_disable_removes_agent_templates(self) -> None:
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg)

        class AgentPlugin(_TestPlugin):
            def get_agents(self):
                return [{"agent_id": "temp"}]

        plugin = AgentPlugin("agent_plugin")
        preg.register(plugin)
        preg.set_status("agent_plugin", PluginStatus.LOADED)
        lm.enable("agent_plugin")
        assert lm.list_agent_templates() != {}

        lm.disable("agent_plugin")
        assert lm.list_agent_templates() == {}

    def test_nonexistent_plugin_returns_empty(self) -> None:
        preg = PluginRegistry()
        hreg = HookRegistry()
        lm = PluginLifecycleManager(preg, hreg)
        assert lm.get_agent_templates("nonexistent") == []
