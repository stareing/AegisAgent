"""Tests for v4.2 tool_use_summary_tpl integration.

Covers:
- ToolUseSummaryRenderer rendering and fallbacks
- COMPACTABLE_TOOLS constant
- Enhanced SNIP strategy with template summaries
- Backward compatibility (no registry → head+tail fallback)
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_framework.context.tool_use_summary import (
    CLEARED_MESSAGE,
    COMPACTABLE_TOOLS,
    ToolUseSummaryRenderer,
)
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.tool import ToolEntry, ToolMeta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockRegistry:
    """Minimal registry mock for testing ToolUseSummaryRenderer."""

    def __init__(self, tools: dict[str, ToolMeta] | None = None) -> None:
        self._tools = tools or {}

    def get_tool(self, name: str) -> ToolEntry:
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        return ToolEntry(meta=self._tools[name])

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self, **kwargs) -> list[ToolEntry]:
        return [ToolEntry(meta=m) for m in self._tools.values()]


def _make_registry(**tool_templates: str) -> _MockRegistry:
    """Create a mock registry with tools that have tool_use_summary_tpl."""
    tools = {}
    for name, tpl in tool_templates.items():
        tools[name] = ToolMeta(
            name=name,
            description=f"Test {name}",
            tool_use_summary_tpl=tpl,
        )
    return _MockRegistry(tools)


def _make_tool_batch_group(
    tool_calls: list[tuple[str, str, dict]],
    tool_results: list[tuple[str, str, str]],
    group_id: str = "g1",
) -> ToolTransactionGroup:
    """Create a TOOL_BATCH group with assistant tool_calls and tool results.

    tool_calls: [(id, function_name, args), ...]
    tool_results: [(tool_call_id, tool_name, content), ...]
    """
    assistant_msg = Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCallRequest(id=tc_id, function_name=fn, arguments=args)
            for tc_id, fn, args in tool_calls
        ],
    )
    result_msgs = [
        Message(role="tool", content=content, tool_call_id=tc_id, name=name)
        for tc_id, name, content in tool_results
    ]
    all_msgs = [assistant_msg] + result_msgs
    return ToolTransactionGroup(
        group_id=group_id,
        group_type="TOOL_BATCH",
        messages=all_msgs,
        token_estimate=len(str(all_msgs)) // 4,
    )


# ===========================================================================
# ToolUseSummaryRenderer Tests
# ===========================================================================


class TestToolUseSummaryRenderer:
    """Tests for the renderer class."""

    def test_render_basic(self):
        """Template with matching args renders correctly."""
        reg = _make_registry(read_file="Read {path}")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("read_file", {"path": "/foo/bar.py"})
        assert result == "Read /foo/bar.py"

    def test_render_multiple_args(self):
        """Template with multiple args."""
        reg = _make_registry(grep_search="Searched {pattern} in {path}")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("grep_search", {"pattern": "TODO", "path": "src/"})
        assert result == "Searched TODO in src/"

    def test_render_missing_key_fallback(self):
        """Missing arg key uses '...' placeholder, not crash."""
        reg = _make_registry(read_file="Read {path}")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("read_file", {})
        assert result == "Read ..."

    def test_render_non_compactable_returns_none(self):
        """Tool not in COMPACTABLE_TOOLS returns None."""
        reg = _make_registry(enter_plan_mode="Entered plan mode")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("enter_plan_mode", {})
        assert result is None

    def test_render_empty_template_returns_none(self):
        """Empty tool_use_summary_tpl returns None."""
        reg = _make_registry(read_file="")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("read_file", {"path": "/foo"})
        assert result is None

    def test_render_no_registry_returns_none(self):
        """No registry → always None."""
        renderer = ToolUseSummaryRenderer(None)
        result = renderer.render("read_file", {"path": "/foo"})
        assert result is None

    def test_render_unknown_tool_returns_none(self):
        """Tool not in registry → None (not crash)."""
        reg = _make_registry()
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("read_file", {"path": "/foo"})
        assert result is None

    def test_render_no_args_static_template(self):
        """Static template (no placeholders) renders as-is."""
        reg = _make_registry(bash_exec="Ran command")
        renderer = ToolUseSummaryRenderer(reg)
        result = renderer.render("bash_exec", {"command": "ls -la"})
        assert result == "Ran command"


# ===========================================================================
# COMPACTABLE_TOOLS Tests
# ===========================================================================


class TestCompactableTools:
    """Tests for the COMPACTABLE_TOOLS constant."""

    def test_expected_tools_present(self):
        expected = {
            "read_file", "read_many_files", "bash_exec",
            "grep_search", "glob_files", "web_fetch",
            "web_search", "edit_file", "write_file",
        }
        assert expected == COMPACTABLE_TOOLS

    def test_frozen(self):
        """COMPACTABLE_TOOLS is a frozenset."""
        assert isinstance(COMPACTABLE_TOOLS, frozenset)

    def test_cleared_message(self):
        assert CLEARED_MESSAGE == "[Old tool result content cleared]"


# ===========================================================================
# Enhanced SNIP Strategy Tests
# ===========================================================================


class TestSnipWithTemplateSummary:
    """Tests for the enhanced SNIP strategy using template summaries."""

    def test_snip_with_registry_uses_template(self):
        """With registry, SNIP uses template summary instead of head+tail."""
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        long_content = "x" * 1000
        group = _make_tool_batch_group(
            tool_calls=[("tc1", "read_file", {"path": "/big.txt"})],
            tool_results=[("tc1", "read_file", long_content)],
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        assert len(result) == 1
        tool_msg = result[0].messages[1]  # second msg is tool result
        assert tool_msg.content == "[Tool summary: Read /big.txt]"

    def test_snip_without_registry_uses_head_tail(self):
        """Without registry, SNIP uses classic head+tail truncation."""
        compressor = ContextCompressor(strategy="SNIP")

        long_content = "A" * 200 + "B" * 600 + "C" * 200
        group = _make_tool_batch_group(
            tool_calls=[("tc1", "read_file", {"path": "/big.txt"})],
            tool_results=[("tc1", "read_file", long_content)],
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        tool_msg = result[0].messages[1]
        assert "[content snipped:" in tool_msg.content
        assert tool_msg.content.startswith("A" * 200)

    def test_snip_mixed_compactable_and_non(self):
        """Mixed group: compactable tools get template, others get head+tail."""
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        long_content = "x" * 1000
        group = _make_tool_batch_group(
            tool_calls=[
                ("tc1", "read_file", {"path": "/a.txt"}),
                ("tc2", "spawn_agent", {"task_input": "do stuff"}),
            ],
            tool_results=[
                ("tc1", "read_file", long_content),
                ("tc2", "spawn_agent", long_content),
            ],
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        msgs = result[0].messages
        # read_file gets template summary
        assert msgs[1].content == "[Tool summary: Read /a.txt]"
        # spawn_agent gets head+tail (not in COMPACTABLE_TOOLS)
        assert "[content snipped:" in msgs[2].content

    def test_snip_short_content_not_modified(self):
        """Content shorter than threshold is not modified."""
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        short_content = "small result"
        group = _make_tool_batch_group(
            tool_calls=[("tc1", "read_file", {"path": "/small.txt"})],
            tool_results=[("tc1", "read_file", short_content)],
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        tool_msg = result[0].messages[1]
        assert tool_msg.content == short_content

    def test_snip_template_missing_key_falls_back(self):
        """If template render returns None due to missing key fallback."""
        # Template uses {path} but that's handled by defaultdict
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        long_content = "x" * 1000
        group = _make_tool_batch_group(
            tool_calls=[("tc1", "read_file", {})],  # no path arg
            tool_results=[("tc1", "read_file", long_content)],
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        tool_msg = result[0].messages[1]
        # Should still get template with "..." fallback
        assert tool_msg.content == "[Tool summary: Read ...]"

    def test_snip_no_tool_name_falls_back(self):
        """Tool result without name field falls back to head+tail."""
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        long_content = "x" * 1000
        # Create message without name field
        assistant_msg = Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCallRequest(id="tc1", function_name="read_file", arguments={"path": "/f"})],
        )
        tool_msg = Message(role="tool", content=long_content, tool_call_id="tc1", name=None)
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="TOOL_BATCH",
            messages=[assistant_msg, tool_msg],
            token_estimate=300,
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        assert "[content snipped:" in result[0].messages[1].content

    def test_snip_preserves_non_tool_messages(self):
        """User and assistant messages are never snipped."""
        reg = _make_registry(read_file="Read {path}")
        compressor = ContextCompressor(strategy="SNIP", tool_registry=reg)

        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[
                Message(role="user", content="x" * 1000),
                Message(role="assistant", content="y" * 1000),
            ],
            token_estimate=500,
        )

        result = compressor._snip_tool_outputs([group], target_tokens=100)
        assert result[0].messages[0].content == "x" * 1000
        assert result[0].messages[1].content == "y" * 1000


# ===========================================================================
# Integration: builtin tools have tool_use_summary_tpl
# ===========================================================================


class TestBuiltinToolTemplates:
    """Verify that builtin tools in COMPACTABLE_TOOLS have templates."""

    def test_read_file_has_template(self):
        from agent_framework.tools.builtin.filesystem import read_file
        assert read_file.__tool_meta__.tool_use_summary_tpl == "Read {path}"

    def test_write_file_has_template(self):
        from agent_framework.tools.builtin.filesystem import write_file
        assert write_file.__tool_meta__.tool_use_summary_tpl == "Wrote {path}"

    def test_read_many_files_has_template(self):
        from agent_framework.tools.builtin.filesystem import read_many_files
        assert read_many_files.__tool_meta__.tool_use_summary_tpl == "Read {paths}"

    def test_grep_search_has_template(self):
        from agent_framework.tools.builtin.search import grep_search
        assert grep_search.__tool_meta__.tool_use_summary_tpl == "Searched {pattern} in {path}"

    def test_glob_files_has_template(self):
        from agent_framework.tools.builtin.search import glob_files
        assert glob_files.__tool_meta__.tool_use_summary_tpl == "Found files matching {pattern}"

    def test_bash_exec_has_template(self):
        from agent_framework.tools.builtin.shell import bash_exec
        assert bash_exec.__tool_meta__.tool_use_summary_tpl == "Ran command"

    def test_web_fetch_has_template(self):
        from agent_framework.tools.builtin.web import web_fetch
        assert web_fetch.__tool_meta__.tool_use_summary_tpl == "Fetched {url}"

    def test_web_search_has_template(self):
        from agent_framework.tools.builtin.web import web_search
        assert web_search.__tool_meta__.tool_use_summary_tpl == "Searched web for {query}"

    def test_edit_file_has_template(self):
        from agent_framework.tools.builtin.code_edit import edit_file
        assert edit_file.__tool_meta__.tool_use_summary_tpl == "Edited {file_path}"
