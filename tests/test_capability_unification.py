"""Tests for capability plane unification.

Verifies:
1. Memory admin tools registered and callable via ToolExecutor.execute()
2. CLI slash commands route through ToolExecutor (not direct manager calls)
3. Capability policy blocks memory_admin tools when not allowed
4. Entry.py admin API still works (backward compat)
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.models.message import ToolCallRequest
from agent_framework.models.tool import ToolResult


class TestMemoryAdminTools:
    """Memory admin tools are registered and executable via ToolExecutor."""

    def test_memory_admin_tools_registered(self):
        """list_memories, forget_memory, clear_memories must be in catalog."""
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        names = {e.meta.name for e in catalog.list_all()}
        assert "list_memories" in names
        assert "forget_memory" in names
        assert "clear_memories" in names

    def test_memory_admin_tools_category(self):
        """Memory admin tools must have category='memory_admin'."""
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        for entry in catalog.list_all():
            if entry.meta.name in ("list_memories", "forget_memory", "clear_memories"):
                assert entry.meta.category == "memory_admin"

    def test_list_memories_via_execute(self):
        """list_memories callable through ToolExecutor.execute()."""
        from agent_framework.tools.builtin.memory_admin import \
            set_memory_context

        mock_manager = MagicMock()
        mock_manager.list_memories.return_value = []
        set_memory_context(mock_manager, "test-agent")

        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        from agent_framework.tools.executor import ToolExecutor
        from agent_framework.tools.registry import ToolRegistry

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        registry = ToolRegistry()
        for e in catalog.list_all():
            registry.register(e)

        executor = ToolExecutor(registry=registry)
        req = ToolCallRequest(id="t1", function_name="list_memories", arguments={})
        result, meta = asyncio.run(executor.execute(req))
        assert result.success is True
        assert isinstance(result.output, list)
        mock_manager.list_memories.assert_called_once()

    def test_clear_memories_via_execute(self):
        """clear_memories callable through ToolExecutor.execute()."""
        from agent_framework.tools.builtin.memory_admin import \
            set_memory_context

        mock_manager = MagicMock()
        mock_manager.clear_memories.return_value = 5
        set_memory_context(mock_manager, "test-agent")

        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        from agent_framework.tools.executor import ToolExecutor
        from agent_framework.tools.registry import ToolRegistry

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        registry = ToolRegistry()
        for e in catalog.list_all():
            registry.register(e)

        executor = ToolExecutor(registry=registry)
        req = ToolCallRequest(id="t2", function_name="clear_memories", arguments={})
        result, meta = asyncio.run(executor.execute(req))
        assert result.success is True
        assert "5" in str(result.output)


class TestCapabilityPolicyEnforcement:
    """Memory admin tools must respect capability policy."""

    def test_memory_admin_blocked_by_default_policy(self):
        """Default capability policy (allow_memory_admin=False) blocks memory_admin tools."""
        from agent_framework.agent.capability_policy import \
            apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        all_entries = catalog.list_all()

        # Default policy: allow_memory_admin=False
        policy = CapabilityPolicy()
        assert policy.allow_memory_admin is False
        allowed = apply_capability_policy(all_entries, policy)
        allowed_names = {e.meta.name for e in allowed}
        assert "list_memories" not in allowed_names
        assert "forget_memory" not in allowed_names
        assert "clear_memories" not in allowed_names

    def test_memory_admin_allowed_when_policy_permits(self):
        """When allow_memory_admin=True, memory_admin tools are visible."""
        from agent_framework.agent.capability_policy import \
            apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog)
        all_entries = catalog.list_all()

        policy = CapabilityPolicy(allow_memory_admin=True)
        allowed = apply_capability_policy(all_entries, policy)
        allowed_names = {e.meta.name for e in allowed}
        assert "list_memories" in allowed_names


class TestCLIRoutesThruExecutor:
    """CLI slash commands must route through ToolExecutor.execute()."""

    @pytest.mark.asyncio
    async def test_cmd_memories_uses_execute_tool(self):
        """The /memories command must call _execute_tool (which calls ToolExecutor)."""
        from agent_framework.terminal_runtime import _execute_tool

        mock_fw = MagicMock()
        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=(
            ToolResult(
                tool_call_id="cli_test",
                tool_name="list_memories",
                success=True,
                output=[],
            ),
            MagicMock(execution_time_ms=1),
        ))
        mock_fw._deps = MagicMock()
        mock_fw._deps.tool_executor = mock_executor

        result = await _execute_tool(mock_fw, "list_memories", {"user_id": None})
        assert result.success is True
        mock_executor.execute.assert_called_once()
        # Verify the ToolCallRequest was constructed correctly
        call_args = mock_executor.execute.call_args
        req = call_args[0][0]
        assert req.function_name == "list_memories"

    @pytest.mark.asyncio
    async def test_cmd_memory_clear_uses_execute_tool(self):
        """The /memory-clear command routes through _execute_tool."""
        from agent_framework.terminal_runtime import _execute_tool

        mock_fw = MagicMock()
        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=(
            ToolResult(
                tool_call_id="cli_test",
                tool_name="clear_memories",
                success=True,
                output="Cleared 3 memories",
            ),
            MagicMock(execution_time_ms=1),
        ))
        mock_fw._deps = MagicMock()
        mock_fw._deps.tool_executor = mock_executor

        result = await _execute_tool(mock_fw, "clear_memories", {"user_id": None})
        assert result.success is True
        assert "3" in str(result.output)


class TestAdminAPIBackwardCompat:
    """Entry.py admin API must remain functional."""

    def test_entry_list_memories_still_works(self):
        """AgentFramework.list_memories() must still work directly."""
        from agent_framework.entry import AgentFramework

        fw = AgentFramework()
        fw.setup(auto_approve_tools=True)
        # Should not raise — returns empty list
        records = fw.list_memories()
        assert isinstance(records, list)

    def test_entry_clear_memories_still_works(self):
        """AgentFramework.clear_memories() must still work directly."""
        from agent_framework.entry import AgentFramework

        fw = AgentFramework()
        fw.setup(auto_approve_tools=True)
        count = fw.clear_memories()
        assert isinstance(count, int)
