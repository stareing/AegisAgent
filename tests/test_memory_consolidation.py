"""Tests for v5.0 memory consolidation — dream agent callback."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.memory.consolidation import (
    MAX_MEMORIES_TO_REVIEW,
    ConsolidationResult,
    MemoryConsolidator,
)


@dataclass
class FakeMemory:
    memory_id: str
    title: str
    content: str
    kind: str = "preference"


def _make_store(memories: list[FakeMemory] | None = None):
    store = MagicMock()
    store.list_recent.return_value = memories or []
    store.save.return_value = "new_id"
    return store


def _make_adapter(response_text: str = "[]"):
    adapter = AsyncMock()
    adapter.generate.return_value = response_text
    return adapter


# ===========================================================================
# Basic Consolidation
# ===========================================================================


class TestConsolidate:

    @pytest.mark.asyncio
    async def test_no_memories_returns_empty(self):
        consolidator = MemoryConsolidator(
            store=_make_store([]), adapter=_make_adapter(),
        )
        result = await consolidator.consolidate()
        assert result.created == 0
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_consolidates_with_llm(self):
        memories = [
            FakeMemory("m1", "Prefers dark mode", "User always uses dark mode"),
            FakeMemory("m2", "Uses Python 3.11", "All projects use Python 3.11"),
            FakeMemory("m3", "Dark theme preference", "Dark theme everywhere"),
        ]
        llm_response = json.dumps([
            {
                "title": "Dark mode preference",
                "content": "User consistently prefers dark mode/theme across all applications",
                "kind": "preference",
                "tags": ["ui", "theme"],
                "source_ids": ["m1", "m3"],
            }
        ])
        consolidator = MemoryConsolidator(
            store=_make_store(memories),
            adapter=_make_adapter(llm_response),
        )
        result = await consolidator.consolidate()
        assert result.created == 1
        assert len(result.source_ids) == 3

    @pytest.mark.asyncio
    async def test_llm_returns_empty_array(self):
        memories = [FakeMemory("m1", "One memory", "content")]
        consolidator = MemoryConsolidator(
            store=_make_store(memories),
            adapter=_make_adapter("[]"),
        )
        result = await consolidator.consolidate()
        assert result.created == 0
        assert result.source_ids == ["m1"]

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self):
        memories = [FakeMemory("m1", "test", "content")]
        adapter = AsyncMock()
        adapter.generate.side_effect = RuntimeError("API error")
        consolidator = MemoryConsolidator(
            store=_make_store(memories), adapter=adapter,
        )
        result = await consolidator.consolidate()
        assert len(result.errors) == 1
        assert "LLM call failed" in result.errors[0]

    @pytest.mark.asyncio
    async def test_store_load_failure(self):
        store = MagicMock()
        store.list_recent.side_effect = RuntimeError("DB error")
        consolidator = MemoryConsolidator(
            store=store, adapter=_make_adapter(),
        )
        result = await consolidator.consolidate()
        assert len(result.errors) == 1
        assert "Load failed" in result.errors[0]


# ===========================================================================
# Response Parsing
# ===========================================================================


class TestParseCandidates:

    def _consolidator(self):
        return MemoryConsolidator(
            store=_make_store(), adapter=_make_adapter(),
        )

    def test_parses_json_array(self):
        c = self._consolidator()
        candidates = c._parse_candidates('[{"title": "Test", "content": "c"}]')
        assert len(candidates) == 1
        assert candidates[0]["title"] == "Test"

    def test_parses_markdown_code_block(self):
        c = self._consolidator()
        text = '```json\n[{"title": "Test", "content": "c"}]\n```'
        candidates = c._parse_candidates(text)
        assert len(candidates) == 1

    def test_parses_generic_code_block(self):
        c = self._consolidator()
        text = '```\n[{"title": "Test", "content": "c"}]\n```'
        candidates = c._parse_candidates(text)
        assert len(candidates) == 1

    def test_returns_empty_on_invalid_json(self):
        c = self._consolidator()
        assert c._parse_candidates("not json") == []

    def test_filters_entries_without_title(self):
        c = self._consolidator()
        text = '[{"content": "no title"}, {"title": "has title", "content": "c"}]'
        candidates = c._parse_candidates(text)
        assert len(candidates) == 1
        assert candidates[0]["title"] == "has title"

    def test_returns_empty_on_non_array(self):
        c = self._consolidator()
        assert c._parse_candidates('{"title": "not array"}') == []


# ===========================================================================
# Extract Text
# ===========================================================================


class TestExtractText:

    def _consolidator(self):
        return MemoryConsolidator(
            store=_make_store(), adapter=_make_adapter(),
        )

    def test_from_string(self):
        c = self._consolidator()
        assert c._extract_text("hello") == "hello"

    def test_from_object_with_content(self):
        c = self._consolidator()
        obj = MagicMock()
        obj.content = "response text"
        assert c._extract_text(obj) == "response text"

    def test_from_dict(self):
        c = self._consolidator()
        assert c._extract_text({"content": "test"}) == "test"


# ===========================================================================
# ConsolidationResult
# ===========================================================================


class TestConsolidationResult:

    def test_defaults(self):
        r = ConsolidationResult()
        assert r.merged == 0
        assert r.created == 0
        assert r.skipped == 0
        assert r.source_ids == []
        assert r.errors == []

    def test_frozen(self):
        r = ConsolidationResult(created=5)
        with pytest.raises(AttributeError):
            r.created = 10
