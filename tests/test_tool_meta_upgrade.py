"""Tests for tool system upgrade Phase A+B (v4.1 Claude Code aligned fields)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_framework.models.tool import ToolEntry, ToolExecutionMeta, ToolMeta
from agent_framework.tools.catalog import GlobalToolCatalog
from agent_framework.tools.decorator import tool
from agent_framework.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# ToolMeta new fields: defaults
# ---------------------------------------------------------------------------

class TestToolMetaDefaults:
    """ToolMeta new fields should have correct defaults."""

    def test_new_fields_defaults(self) -> None:
        meta = ToolMeta(name="test_tool")
        assert meta.prompt == ""
        assert meta.aliases == []
        assert meta.search_hint == ""
        assert meta.is_read_only is False
        assert meta.is_destructive is False
        assert meta.always_load is False
        assert meta.max_result_chars == 250_000
        assert meta.activity_description == ""
        assert meta.tool_use_summary_tpl == ""

    def test_new_fields_custom_values(self) -> None:
        meta = ToolMeta(
            name="read_file",
            prompt="Use this to read files",
            aliases=["cat", "view"],
            search_hint="read file content text",
            is_read_only=True,
            is_destructive=False,
            always_load=True,
            max_result_chars=100_000,
            activity_description="Reading file",
            tool_use_summary_tpl="Read {path}",
        )
        assert meta.prompt == "Use this to read files"
        assert meta.aliases == ["cat", "view"]
        assert meta.search_hint == "read file content text"
        assert meta.is_read_only is True
        assert meta.always_load is True
        assert meta.max_result_chars == 100_000
        assert meta.activity_description == "Reading file"
        assert meta.tool_use_summary_tpl == "Read {path}"

    def test_frozen_with_new_fields(self) -> None:
        meta = ToolMeta(name="test", prompt="sys prompt", aliases=["a"])
        with pytest.raises(Exception):
            meta.prompt = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ToolExecutionMeta: activity_description
# ---------------------------------------------------------------------------

class TestToolExecutionMeta:
    def test_activity_description_default(self) -> None:
        meta = ToolExecutionMeta()
        assert meta.activity_description == ""

    def test_activity_description_set(self) -> None:
        meta = ToolExecutionMeta(activity_description="Searching code")
        assert meta.activity_description == "Searching code"


# ---------------------------------------------------------------------------
# @tool decorator passes new fields
# ---------------------------------------------------------------------------

class TestToolDecorator:
    def test_decorator_passes_new_fields(self) -> None:
        @tool(
            name="my_tool",
            description="A tool",
            prompt="System instruction for my_tool",
            aliases=["mt", "mytool"],
            search_hint="my tool keyword search",
            is_read_only=True,
            is_destructive=False,
            should_defer=True,
            always_load=False,
            max_result_chars=500,
            activity_description="Running my_tool",
            tool_use_summary_tpl="Executed my_tool",
        )
        def my_tool(x: int) -> int:
            return x

        meta: ToolMeta = my_tool.__tool_meta__  # type: ignore[attr-defined]
        assert meta.prompt == "System instruction for my_tool"
        assert meta.aliases == ["mt", "mytool"]
        assert meta.search_hint == "my tool keyword search"
        assert meta.is_read_only is True
        assert meta.is_destructive is False
        assert meta.should_defer is True
        assert meta.always_load is False
        assert meta.max_result_chars == 500
        assert meta.activity_description == "Running my_tool"
        assert meta.tool_use_summary_tpl == "Executed my_tool"

    def test_decorator_defaults_new_fields(self) -> None:
        @tool(name="simple")
        def simple() -> str:
            return "ok"

        meta: ToolMeta = simple.__tool_meta__  # type: ignore[attr-defined]
        assert meta.prompt == ""
        assert meta.aliases == []
        assert meta.max_result_chars == 250_000


# ---------------------------------------------------------------------------
# ToolRegistry: alias registration and lookup
# ---------------------------------------------------------------------------

class TestRegistryAliases:
    def _make_entry(
        self, name: str, aliases: list[str] | None = None, prompt: str = ""
    ) -> ToolEntry:
        meta = ToolMeta(
            name=name,
            aliases=aliases or [],
            prompt=prompt,
        )
        return ToolEntry(meta=meta, callable_ref=lambda: None)

    def test_register_and_lookup_by_alias(self) -> None:
        reg = ToolRegistry()
        entry = self._make_entry("read_file", aliases=["cat", "view"])
        reg.register(entry)
        # Lookup by bare name
        assert reg.has_tool("read_file")
        assert reg.get_tool("read_file") is entry
        # Lookup by alias
        assert reg.has_tool("cat")
        assert reg.get_tool("cat") is entry
        assert reg.has_tool("view")
        assert reg.get_tool("view") is entry

    def test_remove_cleans_aliases(self) -> None:
        reg = ToolRegistry()
        entry = self._make_entry("edit_file", aliases=["edit"])
        reg.register(entry)
        assert reg.has_tool("edit")
        reg.remove("edit_file")
        assert not reg.has_tool("edit_file")
        assert not reg.has_tool("edit")


# ---------------------------------------------------------------------------
# ToolRegistry: export_schemas includes prompt in description
# ---------------------------------------------------------------------------

class TestRegistryExportSchemas:
    def test_prompt_appended_to_description(self) -> None:
        reg = ToolRegistry()
        meta = ToolMeta(
            name="grep",
            description="Search files",
            prompt="Always use regex syntax",
        )
        entry = ToolEntry(meta=meta, callable_ref=lambda: None)
        reg.register(entry)
        schemas = reg.export_schemas()
        assert len(schemas) == 1
        desc = schemas[0]["function"]["description"]
        assert "Search files" in desc
        assert "Always use regex syntax" in desc

    def test_no_prompt_keeps_description(self) -> None:
        reg = ToolRegistry()
        meta = ToolMeta(name="ls", description="List files")
        entry = ToolEntry(meta=meta, callable_ref=lambda: None)
        reg.register(entry)
        schemas = reg.export_schemas()
        assert schemas[0]["function"]["description"] == "List files"

    def test_prompt_only_no_description(self) -> None:
        reg = ToolRegistry()
        meta = ToolMeta(name="x", prompt="Only prompt")
        entry = ToolEntry(meta=meta, callable_ref=lambda: None)
        reg.register(entry)
        schemas = reg.export_schemas()
        assert schemas[0]["function"]["description"] == "Only prompt"


# ---------------------------------------------------------------------------
# GlobalToolCatalog: alias lookup
# ---------------------------------------------------------------------------

class TestCatalogAliasLookup:
    def test_get_by_alias(self) -> None:
        catalog = GlobalToolCatalog()
        meta = ToolMeta(name="write_file", aliases=["write", "save"])
        entry = ToolEntry(meta=meta, callable_ref=lambda: None)
        catalog.register(entry)
        # Direct qualified name lookup
        assert catalog.get("local::write_file") is entry
        # Alias lookup (not qualified, falls back to alias scan)
        assert catalog.get("write") is entry
        assert catalog.get("save") is entry

    def test_get_unknown_returns_none(self) -> None:
        catalog = GlobalToolCatalog()
        assert catalog.get("nonexistent") is None


# ---------------------------------------------------------------------------
# Executor: result truncation at max_result_chars
# ---------------------------------------------------------------------------

class TestExecutorTruncation:
    def test_truncation_at_max_result_chars(self) -> None:
        """Simulate truncation logic from executor.execute()."""
        # Replicate the truncation logic inline to unit-test it
        max_chars = 100
        output = "x" * 200
        if isinstance(output, str) and len(output) > max_chars:
            output = output[:max_chars] + f"\n... [truncated, {len(output)} total chars]"
        assert len(output) > max_chars  # includes suffix
        assert output.startswith("x" * 100)
        assert "[truncated, 200 total chars]" in output

    def test_no_truncation_under_limit(self) -> None:
        max_chars = 250_000
        output = "short"
        if isinstance(output, str) and len(output) > max_chars:
            output = output[:max_chars] + "\n... [truncated]"
        assert output == "short"

    @pytest.mark.asyncio
    async def test_execute_truncates_output(self) -> None:
        """Integration test: executor truncates long output per ToolMeta.max_result_chars."""
        from agent_framework.models.message import ToolCallRequest

        max_chars = 50
        long_output = "a" * 200

        @tool(name="big_tool", description="Returns big output", max_result_chars=max_chars)
        def big_tool() -> str:
            return long_output

        meta: ToolMeta = big_tool.__tool_meta__  # type: ignore[attr-defined]
        validator = big_tool.__tool_validator__  # type: ignore[attr-defined]
        entry = ToolEntry(meta=meta, callable_ref=big_tool, validator_model=validator)

        registry = ToolRegistry()
        registry.register(entry)

        from agent_framework.tools.executor import ToolExecutor

        executor = ToolExecutor(registry=registry)

        req = ToolCallRequest(id="call_1", function_name="big_tool", arguments={})
        result, exec_meta = await executor.execute(req)
        assert result.success is True
        assert isinstance(result.output, str)
        # Output should be truncated: 50 chars + suffix
        assert result.output.startswith("a" * 50)
        assert "[truncated," in result.output
        # activity_description should be empty (default)
        assert exec_meta.activity_description == ""

    @pytest.mark.asyncio
    async def test_execute_activity_description_propagated(self) -> None:
        """ToolExecutionMeta.activity_description is copied from ToolMeta."""
        from agent_framework.models.message import ToolCallRequest

        @tool(name="act_tool", description="desc", activity_description="Doing stuff")
        def act_tool() -> str:
            return "ok"

        meta: ToolMeta = act_tool.__tool_meta__  # type: ignore[attr-defined]
        validator = act_tool.__tool_validator__  # type: ignore[attr-defined]
        entry = ToolEntry(meta=meta, callable_ref=act_tool, validator_model=validator)

        registry = ToolRegistry()
        registry.register(entry)

        from agent_framework.tools.executor import ToolExecutor

        executor = ToolExecutor(registry=registry)
        req = ToolCallRequest(id="call_2", function_name="act_tool", arguments={})
        _result, exec_meta = await executor.execute(req)
        assert exec_meta.activity_description == "Doing stuff"
