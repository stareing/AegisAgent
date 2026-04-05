"""System prompt section registry — independent caching per section.

Each section of the system prompt can be independently computed and cached.
Cached sections persist across turns (hash-based invalidation).
Volatile sections recompute every turn (may break KV cache).

Aligns with Claude Code's systemPromptSections.ts pattern.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

SectionCompute = Callable[[], str | None]


@dataclass(frozen=True)
class PromptSection:
    """A single system prompt section with independent cache control."""

    name: str
    compute: SectionCompute
    cache_break: bool = False  # True = recompute every turn (volatile)


def prompt_section(name: str, compute: SectionCompute) -> PromptSection:
    """Create a cached prompt section. Computed once, reused until invalidated."""
    return PromptSection(name=name, compute=compute, cache_break=False)


def volatile_section(name: str, compute: SectionCompute, reason: str) -> PromptSection:
    """Create a volatile prompt section. Recomputes every turn, may break cache.

    Args:
        name: Section name.
        compute: Function returning section text or None.
        reason: Why this section must be volatile (documentation only).
    """
    return PromptSection(name=name, compute=compute, cache_break=bool(reason))


class PromptSectionRegistry:
    """Manages system prompt sections with per-section caching.

    Cached sections (cache_break=False):
    - Computed once on first access
    - Cached value reused on subsequent calls
    - Cache entry invalidated only by invalidate() or invalidate_section()
    - Hash includes only cached section values (stable prefix)

    Volatile sections (cache_break=True):
    - Recomputed every call to resolve_all()
    - NOT included in prefix hash (changes don't trigger rotation)
    - Placed AFTER cached sections in output (minimize cache invalidation)
    """

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._cache: dict[str, str | None] = {}

    def register(self, section: PromptSection) -> None:
        """Register a prompt section."""
        self._sections.append(section)

    def resolve_all(self) -> tuple[list[str], list[str]]:
        """Resolve all sections, returning (cached_parts, volatile_parts).

        Cached sections use stored values when available.
        Volatile sections are always recomputed.

        Returns:
            Tuple of (cached_section_texts, volatile_section_texts).
            Each list contains non-None section outputs.
        """
        cached_parts: list[str] = []
        volatile_parts: list[str] = []

        for section in self._sections:
            if section.cache_break:
                # Volatile: always recompute
                value = section.compute()
                if value:
                    volatile_parts.append(value)
            else:
                # Cached: use stored value if available
                if section.name in self._cache:
                    value = self._cache[section.name]
                else:
                    value = section.compute()
                    self._cache[section.name] = value
                if value:
                    cached_parts.append(value)

        return cached_parts, volatile_parts

    def compute_cached_hash(self) -> str:
        """Compute hash of all cached section values.

        Only cached sections participate in the hash.
        Volatile sections are excluded (they change every turn).
        This is used by PromptPrefixManager for rotation decisions.
        """
        parts: list[str] = []
        for section in self._sections:
            if not section.cache_break and section.name in self._cache:
                value = self._cache.get(section.name)
                if value:
                    parts.append(f"{section.name}:{value}")
        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def invalidate(self) -> None:
        """Clear all cached section values. Called on /compact or /clear."""
        self._cache.clear()

    def invalidate_section(self, name: str) -> None:
        """Invalidate a specific section's cache."""
        self._cache.pop(name, None)

    @property
    def section_count(self) -> int:
        return len(self._sections)

    @property
    def cached_section_count(self) -> int:
        return sum(1 for s in self._sections if not s.cache_break)

    @property
    def volatile_section_count(self) -> int:
        return sum(1 for s in self._sections if s.cache_break)
