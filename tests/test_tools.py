"""Strict unit tests for the tools layer.

Covers: decorator, catalog, registry, executor, confirmation, delegation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.models.agent import CapabilityPolicy
from agent_framework.models.message import ToolCallRequest
from agent_framework.models.subagent import SubAgentResult, SubAgentSpec
from agent_framework.models.tool import (ToolEntry, ToolExecutionError,
                                         ToolMeta, ToolResult)
from agent_framework.tools.catalog import GlobalToolCatalog
from agent_framework.tools.confirmation import (AutoApproveConfirmationHandler,
                                                CLIConfirmationHandler)
from agent_framework.tools.decorator import (_build_parameters_model,
                                             _extract_description, tool)
from agent_framework.tools.delegation import DelegationExecutor
from agent_framework.tools.executor import ToolExecutor
from agent_framework.tools.registry import (ScopedToolRegistry, ToolRegistry,
                                            _qualified_name)

# =====================================================================
# @tool decorator
# =====================================================================


class TestToolDecorator:
    def test_basic_decoration(self):
        @tool()
        def greet(name: str) -> str:
            """Say hello."""
            return f"Hello, {name}"

        assert hasattr(greet, "__tool_meta__")
        meta: ToolMeta = greet.__tool_meta__
        assert meta.name == "greet"
        assert meta.description == "Say hello."
        assert meta.source == "local"
        assert meta.is_async is False
        assert meta.category == "general"

    def test_custom_name_and_category(self):
        @tool(name="my_tool", category="filesystem", require_confirm=True, tags=["io"])
        def read_file(path: str) -> str:
            """Read a file."""
            return ""

        meta = read_file.__tool_meta__
        assert meta.name == "my_tool"
        assert meta.category == "filesystem"
        assert meta.require_confirm is True
        assert meta.tags == ["io"]

    def test_async_detection(self):
        @tool()
        async def async_tool(x: int) -> int:
            return x * 2

        assert async_tool.__tool_meta__.is_async is True

    def test_description_from_override(self):
        @tool(description="Custom description")
        def f():
            """This docstring should be ignored."""
            pass

        assert f.__tool_meta__.description == "Custom description"

    def test_description_from_docstring_first_paragraph(self):
        @tool()
        def f():
            """First paragraph.

            Second paragraph with details.
            """
            pass

        assert f.__tool_meta__.description == "First paragraph."

    def test_no_description(self):
        @tool()
        def f():
            pass

        assert f.__tool_meta__.description == ""

    def test_parameter_schema_generation(self):
        @tool()
        def calc(expression: str, precision: int = 2) -> float:
            """Calculate."""
            return 0.0

        meta = calc.__tool_meta__
        schema = meta.parameters_schema
        assert "properties" in schema
        assert "expression" in schema["properties"]
        assert "precision" in schema["properties"]
        # expression is required (no default), precision has default
        assert "expression" in schema.get("required", [])

    def test_no_parameters(self):
        @tool()
        def noop() -> None:
            pass

        assert noop.__tool_meta__.parameters_schema == {}

    def test_validator_model_created(self):
        @tool()
        def f(x: int, y: str = "default") -> None:
            pass

        assert f.__tool_validator__ is not None
        obj = f.__tool_validator__(x=42)
        assert obj.x == 42
        assert obj.y == "default"

    def test_decorated_function_still_callable(self):
        @tool()
        def add(a: int, b: int) -> int:
            return a + b

        assert add(1, 2) == 3

    def test_namespace_parameter(self):
        @tool(namespace="custom_ns")
        def f():
            pass

        assert f.__tool_meta__.namespace == "custom_ns"


class TestBuildParametersModel:
    def test_self_parameter_skipped(self):
        class Foo:
            def method(self, x: int) -> int:
                return x

        schema, model = _build_parameters_model(Foo.method)
        assert "self" not in schema.get("properties", {})

    def test_empty_function(self):
        def f():
            pass

        schema, model = _build_parameters_model(f)
        assert schema == {}
        assert model is None


class TestExtractDescription:
    def test_override_takes_precedence(self):
        def f():
            """Docstring."""
            pass

        assert _extract_description(f, "Override") == "Override"

    def test_no_docstring_returns_empty(self):
        def f():
            pass

        assert _extract_description(f, None) == ""


# =====================================================================
# GlobalToolCatalog
# =====================================================================


class TestGlobalToolCatalog:
    def _make_entry(self, name="test_tool", source="local", category="general"):
        meta = ToolMeta(name=name, description="desc", source=source, category=category)
        return ToolEntry(meta=meta, callable_ref=lambda: None)

    def test_register_and_get(self):
        catalog = GlobalToolCatalog()
        entry = self._make_entry("calc")
        catalog.register(entry)
        retrieved = catalog.get("local::calc")
        assert retrieved is not None
        assert retrieved.meta.name == "calc"

    def test_register_function(self):
        catalog = GlobalToolCatalog()

        @tool()
        def my_func(x: int) -> int:
            return x

        catalog.register_function(my_func)
        assert catalog.get("local::my_func") is not None

    def test_register_function_without_decorator_raises(self):
        catalog = GlobalToolCatalog()
        with pytest.raises(ValueError, match="not decorated"):
            catalog.register_function(lambda: None)

    def test_register_module(self):
        import types
        mod = types.ModuleType("test_mod")

        @tool()
        def tool_a() -> None:
            pass

        @tool()
        def tool_b() -> None:
            pass

        mod.tool_a = tool_a
        mod.tool_b = tool_b

        catalog = GlobalToolCatalog()
        count = catalog.register_module(mod)
        assert count == 2

    def test_unregister(self):
        catalog = GlobalToolCatalog()
        entry = self._make_entry("temp")
        catalog.register(entry)
        assert catalog.unregister("local::temp") is True
        assert catalog.get("local::temp") is None

    def test_unregister_nonexistent(self):
        catalog = GlobalToolCatalog()
        assert catalog.unregister("nonexistent") is False

    def test_list_all(self):
        catalog = GlobalToolCatalog()
        catalog.register(self._make_entry("a"))
        catalog.register(self._make_entry("b"))
        assert len(catalog.list_all()) == 2

    def test_qualified_name_mcp(self):
        catalog = GlobalToolCatalog()
        meta = ToolMeta(name="search", source="mcp", mcp_server_id="server1")
        entry = ToolEntry(meta=meta)
        catalog.register(entry)
        assert catalog.get("mcp::server1::search") is not None

    def test_qualified_name_a2a(self):
        catalog = GlobalToolCatalog()
        meta = ToolMeta(name="ask", source="a2a", a2a_agent_url="http://host/agent1")
        entry = ToolEntry(meta=meta)
        catalog.register(entry)
        assert catalog.get("a2a::agent1::ask") is not None

    def test_qualified_name_subagent(self):
        catalog = GlobalToolCatalog()
        meta = ToolMeta(name="spawn_agent", source="subagent")
        entry = ToolEntry(meta=meta)
        catalog.register(entry)
        assert catalog.get("subagent::spawn_agent") is not None

    def test_overwrite_warns(self):
        catalog = GlobalToolCatalog()
        catalog.register(self._make_entry("dup"))
        catalog.register(self._make_entry("dup"))  # should overwrite
        assert len(catalog.list_all()) == 1


# =====================================================================
# ToolRegistry & ScopedToolRegistry
# =====================================================================


class TestToolRegistry:
    def _make_entry(self, name, source="local", category="general", tags=None):
        meta = ToolMeta(name=name, source=source, category=category, tags=tags or [])
        return ToolEntry(meta=meta, callable_ref=lambda: None)

    def test_register_and_lookup_by_bare_name(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("calc"))
        assert reg.has_tool("calc")
        entry = reg.get_tool("calc")
        assert entry.meta.name == "calc"

    def test_lookup_by_qualified_name(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("calc"))
        entry = reg.get_tool("local::calc")
        assert entry.meta.name == "calc"

    def test_lookup_not_found_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError, match="Tool not found"):
            reg.get_tool("nonexistent")

    def test_remove(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("temp"))
        assert reg.remove("temp") is True
        assert reg.has_tool("temp") is False

    def test_remove_nonexistent(self):
        reg = ToolRegistry()
        assert reg.remove("nope") is False

    def test_list_tools_filter_by_category(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("a", category="fs"))
        reg.register(self._make_entry("b", category="net"))
        reg.register(self._make_entry("c", category="fs"))
        result = reg.list_tools(category="fs")
        assert len(result) == 2

    def test_list_tools_filter_by_tags(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("a", tags=["io", "fast"]))
        reg.register(self._make_entry("b", tags=["slow"]))
        result = reg.list_tools(tags=["io"])
        assert len(result) == 1

    def test_list_tools_filter_by_source(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("a", source="local"))
        reg.register(self._make_entry("b", source="mcp"))
        result = reg.list_tools(source="mcp")
        assert len(result) == 1

    def test_export_schemas(self):
        reg = ToolRegistry()
        meta = ToolMeta(
            name="calc", description="Calculate", source="local",
            parameters_schema={"type": "object", "properties": {"expr": {"type": "string"}}},
        )
        reg.register(ToolEntry(meta=meta))
        schemas = reg.export_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "calc"
        assert schemas[0]["function"]["parameters"]["type"] == "object"

    def test_export_schemas_with_whitelist(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("a"))
        reg.register(self._make_entry("b"))
        schemas = reg.export_schemas(whitelist=["a"])
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "a"

    def test_snapshot(self):
        reg = ToolRegistry()
        reg.register(self._make_entry("x"))
        snap = reg.snapshot()
        assert "local::x" in snap

    def test_add_alias(self):
        reg = ToolRegistry()
        entry = self._make_entry("t")
        reg.add(entry)  # alias for register
        assert reg.has_tool("t")


class TestScopedToolRegistry:
    def _make_registry(self):
        reg = ToolRegistry()
        for name, cat in [("a", "fs"), ("b", "net"), ("c", "fs"), ("d", "system")]:
            meta = ToolMeta(name=name, source="local", category=cat)
            reg.register(ToolEntry(meta=meta, callable_ref=lambda: None))
        return reg

    def test_whitelist_filters(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=["a", "c"])
        assert scoped.has_tool("a")
        assert scoped.has_tool("c")
        assert not scoped.has_tool("b")
        assert not scoped.has_tool("d")

    def test_no_whitelist_includes_all(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=None)
        assert len(scoped.list_tools()) == 4

    def test_get_tool_not_in_scope(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=["a"])
        with pytest.raises(KeyError, match="not found in scope"):
            scoped.get_tool("b")

    def test_list_tools_filter_by_category(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=["a", "b", "c"])
        assert len(scoped.list_tools(category="fs")) == 2

    def test_export_schemas(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=["a"])
        schemas = scoped.export_schemas()
        assert len(schemas) == 1

    def test_lookup_by_qualified_name(self):
        reg = self._make_registry()
        scoped = ScopedToolRegistry(reg, whitelist=["a"])
        entry = scoped.get_tool("local::a")
        assert entry.meta.name == "a"


class TestQualifiedName:
    def test_local(self):
        meta = ToolMeta(name="f", source="local")
        assert _qualified_name(meta) == "local::f"

    def test_mcp(self):
        meta = ToolMeta(name="search", source="mcp", mcp_server_id="srv1")
        assert _qualified_name(meta) == "mcp::srv1::search"

    def test_a2a(self):
        meta = ToolMeta(name="ask", source="a2a", a2a_agent_url="http://x/agent")
        assert _qualified_name(meta) == "a2a::agent::ask"

    def test_a2a_no_url(self):
        meta = ToolMeta(name="ask", source="a2a")
        assert _qualified_name(meta) == "a2a::unknown::ask"

    def test_subagent(self):
        meta = ToolMeta(name="spawn_agent", source="subagent")
        assert _qualified_name(meta) == "subagent::spawn_agent"


# =====================================================================
# ToolExecutor
# =====================================================================


class TestToolExecutor:
    def _make_registry_with_tool(self, name="calc", is_async=False, require_confirm=False, category="general"):
        reg = ToolRegistry()
        if is_async:
            async def fn(**kw):
                return 42
        else:
            def fn(**kw):
                return 42

        meta = ToolMeta(
            name=name, source="local", is_async=is_async,
            require_confirm=require_confirm, category=category,
        )
        reg.register(ToolEntry(meta=meta, callable_ref=fn))
        return reg

    @pytest.mark.asyncio
    async def test_execute_sync_tool(self):
        reg = self._make_registry_with_tool("calc", is_async=False)
        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="calc", arguments={})
        result, meta = await executor.execute(req)
        assert result.success is True
        assert result.output == 42
        assert meta.source == "local"

    @pytest.mark.asyncio
    async def test_execute_async_tool(self):
        reg = self._make_registry_with_tool("calc", is_async=True)
        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="calc", arguments={})
        result, meta = await executor.execute(req)
        assert result.success is True
        assert result.output == 42

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        reg = ToolRegistry()
        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="nonexistent", arguments={})
        result, meta = await executor.execute(req)
        assert result.success is False
        assert result.error.error_type == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_confirmation_denied(self):
        reg = self._make_registry_with_tool("danger", require_confirm=True)
        deny_handler = MagicMock()
        deny_handler.request_confirmation = AsyncMock(return_value=False)
        executor = ToolExecutor(registry=reg, confirmation_handler=deny_handler)
        req = ToolCallRequest(id="c1", function_name="danger", arguments={})
        result, _ = await executor.execute(req)
        assert result.success is False
        assert result.error.error_type == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_confirmation_approved(self):
        reg = self._make_registry_with_tool("danger", require_confirm=True)
        approve_handler = MagicMock()
        approve_handler.request_confirmation = AsyncMock(return_value=True)
        executor = ToolExecutor(registry=reg, confirmation_handler=approve_handler)
        req = ToolCallRequest(id="c1", function_name="danger", arguments={})
        result, _ = await executor.execute(req)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_validation_error(self):
        reg = ToolRegistry()

        @tool()
        def typed_tool(x: int) -> int:
            return x

        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        catalog.register_function(typed_tool)
        for e in catalog.list_all():
            reg.register(e)

        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="typed_tool", arguments={"x": "not_an_int"})
        result, _ = await executor.execute(req)
        # pydantic v2 coerces "not_an_int" to fail
        assert result.success is False
        assert result.error.error_type == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_execution_runtime_error(self):
        reg = ToolRegistry()

        def failing(**kw):
            raise RuntimeError("boom")

        meta = ToolMeta(name="bad", source="local")
        reg.register(ToolEntry(meta=meta, callable_ref=failing))

        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="bad", arguments={})
        result, _ = await executor.execute(req)
        assert result.success is False
        assert "boom" in result.error.message

    @pytest.mark.asyncio
    async def test_batch_execute(self):
        reg = self._make_registry_with_tool("calc", is_async=True)
        executor = ToolExecutor(registry=reg, max_concurrent=2)
        reqs = [
            ToolCallRequest(id=f"c{i}", function_name="calc", arguments={})
            for i in range(3)
        ]
        results = await executor.batch_execute(reqs)
        assert len(results) == 3
        assert all(r[0].success for r in results)

    @pytest.mark.asyncio
    async def test_mcp_route_not_configured(self):
        reg = ToolRegistry()
        meta = ToolMeta(name="search", source="mcp", mcp_server_id="srv1")
        reg.register(ToolEntry(meta=meta))
        executor = ToolExecutor(registry=reg, mcp_client_manager=None)
        req = ToolCallRequest(id="c1", function_name="search", arguments={})
        result, _ = await executor.execute(req)
        assert result.success is False
        assert "MCPClientManager not configured" in result.error.message

    @pytest.mark.asyncio
    async def test_unknown_source_raises(self):
        """When _route_execution hits an unknown source, error is caught and returned."""
        reg = ToolRegistry()
        meta = ToolMeta(name="mystery", source="local")
        entry = ToolEntry(meta=meta, callable_ref=None)
        reg.register(entry)
        executor = ToolExecutor(registry=reg)
        req = ToolCallRequest(id="c1", function_name="mystery", arguments={})
        # local source with callable_ref=None triggers RuntimeError
        result, exec_meta = await executor.execute(req)
        assert result.success is False
        assert "No callable" in result.error.message

    def test_is_tool_allowed(self):
        reg = self._make_registry_with_tool("calc", category="general")
        executor = ToolExecutor(registry=reg)
        policy = CapabilityPolicy(allowed_tool_categories=["general"])
        assert executor.is_tool_allowed("calc", policy) is True

    def test_is_tool_not_allowed(self):
        reg = self._make_registry_with_tool("calc", category="general")
        executor = ToolExecutor(registry=reg)
        policy = CapabilityPolicy(allowed_tool_categories=["filesystem"])
        assert executor.is_tool_allowed("calc", policy) is False


# =====================================================================
# Confirmation handlers
# =====================================================================


class TestConfirmationHandlers:
    @pytest.mark.asyncio
    async def test_auto_approve_always_true(self):
        handler = AutoApproveConfirmationHandler()
        assert await handler.request_confirmation("tool", {}, "desc") is True

    @pytest.mark.asyncio
    async def test_cli_handler_yes(self):
        handler = CLIConfirmationHandler()
        with patch("builtins.input", return_value="y"):
            assert await handler.request_confirmation("tool", {}, "desc") is True

    @pytest.mark.asyncio
    async def test_cli_handler_no(self):
        handler = CLIConfirmationHandler()
        with patch("builtins.input", return_value="n"):
            assert await handler.request_confirmation("tool", {}, "desc") is False

    @pytest.mark.asyncio
    async def test_cli_handler_empty_is_no(self):
        handler = CLIConfirmationHandler()
        with patch("builtins.input", return_value=""):
            assert await handler.request_confirmation("tool", {}, "desc") is False


# =====================================================================
# DelegationExecutor
# =====================================================================


class TestDelegationExecutor:
    @pytest.mark.asyncio
    async def test_no_runtime_returns_error(self):
        de = DelegationExecutor(sub_agent_runtime=None)
        spec = SubAgentSpec(task_input="test")
        result = await de.delegate_to_subagent(spec, None)
        assert result.success is False
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_parent_hook_denies_spawn(self):
        from agent_framework.models.agent import SpawnDecision
        runtime = MagicMock()
        de = DelegationExecutor(sub_agent_runtime=runtime)

        parent = MagicMock()
        parent.on_spawn_requested = AsyncMock(
            return_value=SpawnDecision(allowed=False, reason="test deny")
        )

        spec = SubAgentSpec(task_input="test")
        result = await de.delegate_to_subagent(spec, parent)
        assert result.success is False
        assert "PERMISSION_DENIED" in result.error

    @pytest.mark.asyncio
    async def test_allow_spawn_children_false_denies(self):
        from agent_framework.models.agent import SpawnDecision
        runtime = MagicMock()
        de = DelegationExecutor(sub_agent_runtime=runtime)

        parent = MagicMock()
        parent.on_spawn_requested = AsyncMock(
            return_value=SpawnDecision(allowed=True)
        )
        parent.agent_config = MagicMock()
        parent.agent_config.allow_spawn_children = False

        spec = SubAgentSpec(task_input="test")
        result = await de.delegate_to_subagent(spec, parent)
        assert result.success is False
        assert "PERMISSION_DENIED" in result.error

    @pytest.mark.asyncio
    async def test_successful_delegation(self):
        from agent_framework.models.agent import SpawnDecision
        expected = SubAgentResult(spawn_id="s1", success=True, final_answer="done")
        runtime = MagicMock()
        runtime.spawn = AsyncMock(return_value=expected)

        de = DelegationExecutor(sub_agent_runtime=runtime)
        parent = MagicMock()
        parent.on_spawn_requested = AsyncMock(
            return_value=SpawnDecision(allowed=True)
        )
        parent.agent_config = MagicMock()
        parent.agent_config.allow_spawn_children = True

        spec = SubAgentSpec(task_input="test")
        result = await de.delegate_to_subagent(spec, parent)
        assert result.success is True
        assert result.final_answer == "done"

    @pytest.mark.asyncio
    async def test_a2a_not_configured(self):
        de = DelegationExecutor()
        result = await de.delegate_to_a2a("http://agent", "task")
        assert result.success is False
        assert "A2A adapter not configured" in result.error

    def test_summarize_result_success(self):
        result = SubAgentResult(spawn_id="s1", success=True, final_answer="answer")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == "COMPLETED"
        assert "answer" in summary.summary
        assert "Do NOT call spawn_agent again" in summary.summary
        assert summary.error_code is None

    def test_summarize_result_failure(self):
        result = SubAgentResult(spawn_id="s1", success=False, error="something went wrong")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == "FAILED"
        assert summary.summary == "something went wrong"
        assert summary.error_code == "DELEGATION_FAILED"

    def test_summarize_result_timeout_error(self):
        result = SubAgentResult(spawn_id="s1", success=False, error="Sub-agent timed out")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.error_code == "TIMEOUT"

    def test_summarize_result_permission_error(self):
        result = SubAgentResult(spawn_id="s1", success=False, error="PERMISSION_DENIED: not allowed")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.error_code == "PERMISSION_DENIED"

    def test_set_a2a_adapter(self):
        de = DelegationExecutor()
        mock_adapter = MagicMock()
        de.set_a2a_adapter(mock_adapter)
        assert de._a2a_adapter is mock_adapter


# =====================================================================
# CapabilityPolicy
# =====================================================================


class TestCapabilityPolicy:
    def _make_tools(self):
        entries = []
        for name, cat in [("read", "fs"), ("write", "fs"), ("http", "network"),
                          ("shell", "system"), ("spawn_agent", "subagent")]:
            meta = ToolMeta(name=name, source="local", category=cat)
            entries.append(ToolEntry(meta=meta))
        return entries

    def test_allow_all_by_default(self):
        tools = self._make_tools()
        # Default policy has allow_spawn=False, so spawn_agent is filtered
        policy = CapabilityPolicy()
        result = apply_capability_policy(tools, policy)
        assert len(result) == 4
        assert not any(t.meta.name == "spawn_agent" for t in result)

    def test_allowed_categories_whitelist(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(allowed_tool_categories=["fs"])
        result = apply_capability_policy(tools, policy)
        assert len(result) == 2
        assert all(t.meta.category == "fs" for t in result)

    def test_blocked_categories(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(blocked_tool_categories=["system", "subagent"])
        result = apply_capability_policy(tools, policy)
        assert not any(t.meta.category in ("system", "subagent") for t in result)

    def test_disallow_network_tools(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(allow_network_tools=False)
        result = apply_capability_policy(tools, policy)
        assert not any(t.meta.category == "network" for t in result)

    def test_disallow_system_tools(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(allow_system_tools=False)
        result = apply_capability_policy(tools, policy)
        assert not any(t.meta.category == "system" for t in result)

    def test_disallow_spawn(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(allow_spawn=False)
        result = apply_capability_policy(tools, policy)
        assert not any(t.meta.name == "spawn_agent" for t in result)

    def test_combined_filters(self):
        tools = self._make_tools()
        policy = CapabilityPolicy(
            allowed_tool_categories=["fs", "network"],
            allow_network_tools=False,
        )
        result = apply_capability_policy(tools, policy)
        # allowed_categories keeps fs+network, then allow_network=False removes network
        assert len(result) == 2
        assert all(t.meta.category == "fs" for t in result)
