"""Tests for ConcurrencyPartitioner — tool concurrency classification and batching."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_framework.models.message import ToolCallRequest
from agent_framework.models.tool import ToolEntry, ToolMeta
from agent_framework.tools.concurrency import (
    ConcurrencyClass,
    ConcurrencyPartitioner,
    ToolCallBatch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(name: str, call_id: str = "") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id or name,
        function_name=name,
        arguments={},
    )


def _make_registry(tool_classes: dict[str, str]) -> MagicMock:
    """Build a mock registry where *tool_classes* maps tool name -> concurrency class."""
    registry = MagicMock()

    def has_tool(name: str) -> bool:
        return name in tool_classes

    def get_tool(name: str) -> ToolEntry:
        cc = tool_classes[name]
        meta = ToolMeta(name=name, concurrency_class=cc)
        return ToolEntry(meta=meta)

    registry.has_tool = MagicMock(side_effect=has_tool)
    registry.get_tool = MagicMock(side_effect=get_tool)
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrencyClass:
    def test_values(self) -> None:
        assert ConcurrencyClass.CONCURRENT_SAFE.value == "concurrent_safe"
        assert ConcurrencyClass.NON_CONCURRENT.value == "non_concurrent"

    def test_str_enum(self) -> None:
        assert ConcurrencyClass("concurrent_safe") == ConcurrencyClass.CONCURRENT_SAFE
        assert ConcurrencyClass("non_concurrent") == ConcurrencyClass.NON_CONCURRENT


class TestToolCallBatch:
    def test_frozen(self) -> None:
        batch = ToolCallBatch(requests=[], concurrent=True)
        assert batch.concurrent is True
        assert batch.requests == []


class TestPartitionMixed:
    """Mixed concurrent / non-concurrent requests produce multiple batches."""

    def test_mixed_sequence(self) -> None:
        registry = _make_registry({
            "read_file": "concurrent_safe",
            "glob_files": "concurrent_safe",
            "write_file": "non_concurrent",
            "grep_search": "concurrent_safe",
        })
        requests = [
            _make_request("read_file"),
            _make_request("glob_files"),
            _make_request("write_file"),
            _make_request("grep_search"),
        ]

        batches = ConcurrencyPartitioner.partition(requests, registry)

        assert len(batches) == 3

        # First batch: two concurrent reads
        assert batches[0].concurrent is True
        assert len(batches[0].requests) == 2
        assert batches[0].requests[0].function_name == "read_file"
        assert batches[0].requests[1].function_name == "glob_files"

        # Second batch: one serial write
        assert batches[1].concurrent is False
        assert len(batches[1].requests) == 1
        assert batches[1].requests[0].function_name == "write_file"

        # Third batch: one concurrent read
        assert batches[2].concurrent is True
        assert len(batches[2].requests) == 1
        assert batches[2].requests[0].function_name == "grep_search"


class TestPartitionAllConcurrent:
    """All concurrent-safe requests produce a single concurrent batch."""

    def test_single_concurrent_batch(self) -> None:
        registry = _make_registry({
            "read_file": "concurrent_safe",
            "glob_files": "concurrent_safe",
            "grep_search": "concurrent_safe",
        })
        requests = [
            _make_request("read_file"),
            _make_request("glob_files"),
            _make_request("grep_search"),
        ]

        batches = ConcurrencyPartitioner.partition(requests, registry)

        assert len(batches) == 1
        assert batches[0].concurrent is True
        assert len(batches[0].requests) == 3


class TestPartitionAllNonConcurrent:
    """All non-concurrent requests produce a single serial batch."""

    def test_single_serial_batch(self) -> None:
        registry = _make_registry({
            "write_file": "non_concurrent",
            "delete_file": "non_concurrent",
        })
        requests = [
            _make_request("write_file"),
            _make_request("delete_file"),
        ]

        batches = ConcurrencyPartitioner.partition(requests, registry)

        assert len(batches) == 1
        assert batches[0].concurrent is False
        assert len(batches[0].requests) == 2


class TestPartitionEmpty:
    """Empty request list produces empty batch list."""

    def test_empty(self) -> None:
        registry = _make_registry({})
        batches = ConcurrencyPartitioner.partition([], registry)
        assert batches == []


class TestPartitionPreservesOrder:
    """Output batches, when flattened, reproduce the original input order."""

    def test_order_preserved(self) -> None:
        registry = _make_registry({
            "a": "concurrent_safe",
            "b": "non_concurrent",
            "c": "concurrent_safe",
            "d": "concurrent_safe",
            "e": "non_concurrent",
        })
        names = ["a", "b", "c", "d", "e"]
        requests = [_make_request(n) for n in names]

        batches = ConcurrencyPartitioner.partition(requests, registry)

        # Flatten and verify ordering
        flat = [r.function_name for batch in batches for r in batch.requests]
        assert flat == names


class TestPartitionUnknownTool:
    """Unknown tools default to non-concurrent (conservative)."""

    def test_unknown_defaults_serial(self) -> None:
        registry = _make_registry({
            "read_file": "concurrent_safe",
        })
        requests = [
            _make_request("read_file"),
            _make_request("unknown_tool"),
        ]

        batches = ConcurrencyPartitioner.partition(requests, registry)

        assert len(batches) == 2
        assert batches[0].concurrent is True
        assert batches[1].concurrent is False


class TestPartitionSingleRequest:
    """A single request produces a single batch."""

    def test_single_concurrent(self) -> None:
        registry = _make_registry({"read_file": "concurrent_safe"})
        batches = ConcurrencyPartitioner.partition(
            [_make_request("read_file")], registry,
        )
        assert len(batches) == 1
        assert batches[0].concurrent is True
        assert len(batches[0].requests) == 1

    def test_single_serial(self) -> None:
        registry = _make_registry({"write_file": "non_concurrent"})
        batches = ConcurrencyPartitioner.partition(
            [_make_request("write_file")], registry,
        )
        assert len(batches) == 1
        assert batches[0].concurrent is False
