"""Tests for deferred tool loading."""

from __future__ import annotations

import pytest

from agent_framework.models.tool import ToolEntry, ToolMeta
from agent_framework.tools.deferred import DeferredToolManager
from agent_framework.tools.registry import ToolRegistry


def _make_entry(
    name: str,
    description: str = "",
    should_defer: bool = False,
) -> ToolEntry:
    """Create a ToolEntry with given parameters."""
    return ToolEntry(
        meta=ToolMeta(
            name=name,
            description=description,
            should_defer=should_defer,
        ),
        callable_ref=lambda: None,
    )


def _build_registry() -> ToolRegistry:
    """Build a registry with a mix of normal and deferred tools."""
    registry = ToolRegistry()
    registry.register(_make_entry("read_file", "Read a file from disk"))
    registry.register(_make_entry("write_file", "Write content to a file"))
    registry.register(
        _make_entry("advanced_search", "Search code with regex", should_defer=True)
    )
    registry.register(
        _make_entry("deploy_service", "Deploy a service to cloud", should_defer=True)
    )
    registry.register(
        _make_entry("run_benchmark", "Run performance benchmarks", should_defer=True)
    )
    return registry


class TestDeferredToolManagerSearch:
    """DeferredToolManager.search finds matching tools."""

    def test_search_finds_by_name(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        results = manager.search("search")
        assert len(results) == 1
        assert results[0]["function"]["name"] == "advanced_search"

    def test_search_finds_by_description(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        results = manager.search("cloud")
        assert len(results) == 1
        assert results[0]["function"]["name"] == "deploy_service"

    def test_search_respects_max_results(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        # All deferred tools match "e" (search, service, benchmark)
        results = manager.search("e", max_results=2)
        assert len(results) <= 2

    def test_search_returns_empty_for_no_match(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        results = manager.search("nonexistent_xyz")
        assert results == []

    def test_search_ignores_non_deferred_tools(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        # "read" matches read_file but it's not deferred
        results = manager.search("read")
        assert len(results) == 0


class TestExportSchemasSkipsDeferred:
    """export_schemas skips deferred tools by default."""

    def test_export_schemas_excludes_deferred(self):
        registry = _build_registry()
        schemas = registry.export_schemas()

        names = [s["function"]["name"] for s in schemas]
        assert "read_file" in names
        assert "write_file" in names
        assert "advanced_search" not in names
        assert "deploy_service" not in names
        assert "run_benchmark" not in names

    def test_export_schemas_includes_deferred_when_requested(self):
        registry = _build_registry()
        schemas = registry.export_schemas(include_deferred=True)

        names = [s["function"]["name"] for s in schemas]
        assert "advanced_search" in names
        assert "deploy_service" in names

    def test_export_schemas_whitelist_overrides_deferred(self):
        registry = _build_registry()
        schemas = registry.export_schemas(whitelist=["advanced_search"])

        names = [s["function"]["name"] for s in schemas]
        assert names == ["advanced_search"]


class TestPromoteMakesToolVisible:
    """promote makes tool visible."""

    def test_promote_marks_tool(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        assert not manager.is_promoted("advanced_search")
        manager.promote("advanced_search")
        assert manager.is_promoted("advanced_search")

    def test_promote_nonexistent_raises(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        with pytest.raises(KeyError):
            manager.promote("nonexistent_tool")

    def test_promote_non_deferred_is_noop(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        # Promoting a non-deferred tool is a no-op (no error)
        manager.promote("read_file")
        assert not manager.is_promoted("read_file")

    def test_reset_clears_promotions(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        manager.promote("advanced_search")
        assert manager.is_promoted("advanced_search")

        manager.reset()
        assert not manager.is_promoted("advanced_search")

    def test_promoted_tools_property(self):
        registry = _build_registry()
        manager = DeferredToolManager(registry)

        manager.promote("advanced_search")
        manager.promote("deploy_service")
        assert manager.promoted_tools == frozenset({"advanced_search", "deploy_service"})
