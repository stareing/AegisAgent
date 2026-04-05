"""Tests for agent_framework.context.prompt_sections."""

from __future__ import annotations

from agent_framework.context.prompt_sections import (
    PromptSection,
    PromptSectionRegistry,
    prompt_section,
    volatile_section,
)


def test_prompt_section_factory() -> None:
    """prompt_section() creates a cached (non-volatile) section."""
    section = prompt_section("identity", lambda: "Hello")
    assert isinstance(section, PromptSection)
    assert section.name == "identity"
    assert section.cache_break is False
    assert section.compute() == "Hello"


def test_volatile_section_factory() -> None:
    """volatile_section() creates a volatile section when reason is provided."""
    section = volatile_section("mcp", lambda: "dynamic", reason="MCP changes per turn")
    assert isinstance(section, PromptSection)
    assert section.name == "mcp"
    assert section.cache_break is True
    assert section.compute() == "dynamic"


def test_registry_resolve_all() -> None:
    """Cached and volatile sections resolve into separate lists."""
    registry = PromptSectionRegistry()
    registry.register(prompt_section("a", lambda: "cached-a"))
    registry.register(volatile_section("b", lambda: "volatile-b", reason="test"))
    registry.register(prompt_section("c", lambda: "cached-c"))

    cached, volatile = registry.resolve_all()

    assert cached == ["cached-a", "cached-c"]
    assert volatile == ["volatile-b"]


def test_registry_caching() -> None:
    """Cached section compute is called only once across multiple resolve_all calls."""
    call_count = 0

    def compute_once() -> str:
        nonlocal call_count
        call_count += 1
        return "value"

    registry = PromptSectionRegistry()
    registry.register(prompt_section("x", compute_once))

    registry.resolve_all()
    registry.resolve_all()
    registry.resolve_all()

    assert call_count == 1


def test_registry_volatile_recomputes() -> None:
    """Volatile section compute is called on every resolve_all."""
    call_count = 0

    def compute_every_time() -> str:
        nonlocal call_count
        call_count += 1
        return f"v{call_count}"

    registry = PromptSectionRegistry()
    registry.register(volatile_section("v", compute_every_time, reason="test"))

    _, vol1 = registry.resolve_all()
    _, vol2 = registry.resolve_all()
    _, vol3 = registry.resolve_all()

    assert call_count == 3
    assert vol1 == ["v1"]
    assert vol2 == ["v2"]
    assert vol3 == ["v3"]


def test_registry_invalidate() -> None:
    """invalidate() clears cache, forcing recompute on next resolve."""
    call_count = 0

    def compute() -> str:
        nonlocal call_count
        call_count += 1
        return f"v{call_count}"

    registry = PromptSectionRegistry()
    registry.register(prompt_section("x", compute))

    registry.resolve_all()
    assert call_count == 1

    registry.invalidate()
    registry.resolve_all()
    assert call_count == 2


def test_registry_invalidate_section() -> None:
    """invalidate_section() clears only the named section's cache."""
    a_count = 0
    b_count = 0

    def compute_a() -> str:
        nonlocal a_count
        a_count += 1
        return "a"

    def compute_b() -> str:
        nonlocal b_count
        b_count += 1
        return "b"

    registry = PromptSectionRegistry()
    registry.register(prompt_section("a", compute_a))
    registry.register(prompt_section("b", compute_b))

    registry.resolve_all()
    assert a_count == 1
    assert b_count == 1

    registry.invalidate_section("a")
    registry.resolve_all()
    assert a_count == 2  # recomputed
    assert b_count == 1  # still cached


def test_compute_cached_hash() -> None:
    """Hash is computed from cached section values only."""
    registry = PromptSectionRegistry()
    registry.register(prompt_section("a", lambda: "value-a"))
    registry.register(prompt_section("b", lambda: "value-b"))

    # Must resolve first to populate cache
    registry.resolve_all()
    h = registry.compute_cached_hash()

    assert isinstance(h, str)
    assert len(h) == 16  # sha256 hex truncated to 16 chars


def test_compute_cached_hash_stable() -> None:
    """Same inputs produce the same hash across calls."""
    registry = PromptSectionRegistry()
    registry.register(prompt_section("x", lambda: "stable"))

    registry.resolve_all()
    h1 = registry.compute_cached_hash()
    h2 = registry.compute_cached_hash()

    assert h1 == h2


def test_compute_cached_hash_excludes_volatile() -> None:
    """Volatile section changes do not affect the cached hash."""
    counter = 0

    def changing_volatile() -> str:
        nonlocal counter
        counter += 1
        return f"volatile-{counter}"

    registry = PromptSectionRegistry()
    registry.register(prompt_section("stable", lambda: "fixed"))
    registry.register(volatile_section("dynamic", changing_volatile, reason="test"))

    registry.resolve_all()
    h1 = registry.compute_cached_hash()

    registry.resolve_all()  # volatile section returns different value
    h2 = registry.compute_cached_hash()

    assert h1 == h2


def test_section_count_properties() -> None:
    """Registry reports correct section counts."""
    registry = PromptSectionRegistry()
    registry.register(prompt_section("a", lambda: "a"))
    registry.register(prompt_section("b", lambda: "b"))
    registry.register(volatile_section("c", lambda: "c", reason="test"))

    assert registry.section_count == 3
    assert registry.cached_section_count == 2
    assert registry.volatile_section_count == 1


def test_none_sections_excluded_from_output() -> None:
    """Sections returning None are excluded from resolved output."""
    registry = PromptSectionRegistry()
    registry.register(prompt_section("present", lambda: "yes"))
    registry.register(prompt_section("absent", lambda: None))
    registry.register(volatile_section("v_absent", lambda: None, reason="test"))

    cached, volatile = registry.resolve_all()

    assert cached == ["yes"]
    assert volatile == []
