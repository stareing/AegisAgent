"""Tests for v4.3 time-based tool result clearing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_framework.context.time_based_clearing import (
    DEFAULT_GAP_THRESHOLD_MINUTES,
    TimeBasedClearing,
)
from agent_framework.context.tool_use_summary import CLEARED_MESSAGE
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message, ToolCallRequest


def _make_group(tool_results: list[tuple[str, str, str]], group_id: str = "g1") -> ToolTransactionGroup:
    """Create a TOOL_BATCH group with tool results.

    tool_results: [(tool_call_id, tool_name, content), ...]
    """
    assistant_msg = Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCallRequest(id=tc_id, function_name=name, arguments={})
            for tc_id, name, _ in tool_results
        ],
    )
    result_msgs = [
        Message(role="tool", content=content, tool_call_id=tc_id, name=name)
        for tc_id, name, content in tool_results
    ]
    return ToolTransactionGroup(
        group_id=group_id,
        group_type="TOOL_BATCH",
        messages=[assistant_msg] + result_msgs,
        token_estimate=100,
    )


class TestShouldTrigger:

    def test_no_assistant_messages(self):
        clearing = TimeBasedClearing()
        msgs = [Message(role="user", content="hello")]
        assert clearing.should_trigger(msgs) is False

    def test_assistant_without_timestamp(self):
        clearing = TimeBasedClearing()
        msgs = [Message(role="assistant", content="hi")]
        assert clearing.should_trigger(msgs) is False

    def test_recent_assistant_no_trigger(self):
        clearing = TimeBasedClearing(gap_threshold_minutes=5)
        now = datetime.now(timezone.utc)
        msgs = [Message(role="assistant", content="hi", metadata={"timestamp": now.isoformat()})]
        assert clearing.should_trigger(msgs) is False

    def test_old_assistant_triggers(self):
        clearing = TimeBasedClearing(gap_threshold_minutes=5)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        msgs = [Message(role="assistant", content="hi", metadata={"timestamp": old.isoformat()})]
        assert clearing.should_trigger(msgs) is True

    def test_uses_last_assistant(self):
        clearing = TimeBasedClearing(gap_threshold_minutes=5)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        msgs = [
            Message(role="assistant", content="old", metadata={"timestamp": old.isoformat()}),
            Message(role="user", content="q"),
            Message(role="assistant", content="recent", metadata={"timestamp": recent.isoformat()}),
        ]
        assert clearing.should_trigger(msgs) is False


class TestClearOldToolResults:

    def test_clears_old_keeps_recent(self):
        clearing = TimeBasedClearing(keep_recent=2)
        groups = [
            _make_group([("tc1", "read_file", "old content 1")], "g1"),
            _make_group([("tc2", "grep_search", "old content 2")], "g2"),
            _make_group([("tc3", "read_file", "recent 1")], "g3"),
            _make_group([("tc4", "read_file", "recent 2")], "g4"),
        ]
        result = clearing.clear_old_tool_results(groups)
        # First 2 tool results cleared, last 2 kept
        assert result[0].messages[1].content == CLEARED_MESSAGE
        assert result[1].messages[1].content == CLEARED_MESSAGE
        assert result[2].messages[1].content == "recent 1"
        assert result[3].messages[1].content == "recent 2"

    def test_non_compactable_not_cleared(self):
        clearing = TimeBasedClearing(keep_recent=1)
        groups = [
            _make_group([("tc1", "spawn_agent", "spawn result")], "g1"),
            _make_group([("tc2", "read_file", "file content")], "g2"),
        ]
        result = clearing.clear_old_tool_results(groups)
        # spawn_agent not in COMPACTABLE_TOOLS → not cleared
        assert result[0].messages[1].content == "spawn result"
        assert result[1].messages[1].content == "file content"

    def test_already_cleared_not_double_cleared(self):
        clearing = TimeBasedClearing(keep_recent=0)
        groups = [
            _make_group([("tc1", "read_file", CLEARED_MESSAGE)], "g1"),
        ]
        result = clearing.clear_old_tool_results(groups)
        # Already cleared → no change
        assert result[0].messages[1].content == CLEARED_MESSAGE

    def test_empty_groups(self):
        clearing = TimeBasedClearing()
        assert clearing.clear_old_tool_results([]) == []

    def test_does_not_mutate_original(self):
        clearing = TimeBasedClearing(keep_recent=1)
        groups = [
            _make_group([("tc1", "read_file", "old content")], "g1"),
            _make_group([("tc2", "read_file", "recent content")], "g2"),
        ]
        result = clearing.clear_old_tool_results(groups)
        assert result[0].messages[1].content == CLEARED_MESSAGE
        assert groups[0].messages[1].content == "old content"  # original unchanged

    def test_keep_recent_floor(self):
        """keep_recent is floored at 1."""
        clearing = TimeBasedClearing(keep_recent=0)
        assert clearing._keep_recent == 1

    def test_multiple_tools_in_one_group(self):
        clearing = TimeBasedClearing(keep_recent=1)
        group = _make_group([
            ("tc1", "read_file", "first"),
            ("tc2", "grep_search", "second"),
        ], "g1")
        result = clearing.clear_old_tool_results([group])
        # Only tc2 (grep_search) is the most recent compactable
        assert result[0].messages[1].content == CLEARED_MESSAGE  # tc1 cleared
        assert result[0].messages[2].content == "second"  # tc2 kept


class TestDefaults:

    def test_default_threshold(self):
        assert DEFAULT_GAP_THRESHOLD_MINUTES == 5.0

    def test_default_keep_recent(self):
        clearing = TimeBasedClearing()
        assert clearing._keep_recent == 3
