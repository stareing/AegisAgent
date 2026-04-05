"""Tests for fork spawning system (Phase 2 — v4.2).

Covers ForkContext model, fork message builders, anti-recursion guard,
and SubAgentSpec fork_context field.
"""

from __future__ import annotations

import pytest

from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.subagent import ForkContext, SubAgentSpec
from agent_framework.subagent.fork import (
    FORK_BOILERPLATE_TAG,
    FORK_DIRECTIVE_PREFIX,
    FORK_PLACEHOLDER_RESULT,
    build_child_directive,
    build_fork_child_messages,
    build_worktree_notice,
    is_in_fork_child,
)


# ---------------------------------------------------------------------------
# ForkContext model
# ---------------------------------------------------------------------------


class TestForkContextModel:
    def test_fork_context_model_frozen(self) -> None:
        ctx = ForkContext(per_child_directive="do task A")
        with pytest.raises(Exception):
            ctx.per_child_directive = "mutated"  # type: ignore[misc]

    def test_fork_context_defaults(self) -> None:
        ctx = ForkContext()
        assert ctx.parent_tool_calls == []
        assert ctx.per_child_directive == ""
        assert ctx.worktree_cwd is None

    def test_fork_context_with_tool_calls(self) -> None:
        tc = ToolCallRequest(id="tc_1", function_name="read_file", arguments={})
        ctx = ForkContext(parent_tool_calls=[tc], per_child_directive="analyze")
        assert len(ctx.parent_tool_calls) == 1
        assert ctx.per_child_directive == "analyze"


# ---------------------------------------------------------------------------
# SubAgentSpec fork_context field
# ---------------------------------------------------------------------------


class TestSubAgentSpecForkContext:
    def test_subagent_spec_fork_context_default_none(self) -> None:
        spec = SubAgentSpec()
        assert spec.fork_context is None

    def test_subagent_spec_fork_context(self) -> None:
        ctx = ForkContext(per_child_directive="task B")
        spec = SubAgentSpec(fork_context=ctx)
        assert spec.fork_context is not None
        assert spec.fork_context.per_child_directive == "task B"


# ---------------------------------------------------------------------------
# build_fork_child_messages
# ---------------------------------------------------------------------------


class TestBuildForkChildMessages:
    def test_build_fork_child_messages_basic(self) -> None:
        """Assistant msg with tool_calls produces correct [assistant, user] structure."""
        tc1 = ToolCallRequest(id="tc_a", function_name="search", arguments={"q": "x"})
        tc2 = ToolCallRequest(id="tc_b", function_name="edit", arguments={"f": "y"})
        assistant_msg = Message(
            role="assistant",
            content="I will search and edit.",
            tool_calls=[tc1, tc2],
        )

        result = build_fork_child_messages(assistant_msg, "Fix the bug in utils.py")

        assert len(result) == 2
        assert result[0] is assistant_msg
        user_msg = result[1]
        assert user_msg.role == "user"
        # Should contain placeholder results for both tool calls
        assert f"[tool_result for search (id=tc_a)]: {FORK_PLACEHOLDER_RESULT}" in user_msg.content
        assert f"[tool_result for edit (id=tc_b)]: {FORK_PLACEHOLDER_RESULT}" in user_msg.content
        # Should contain the directive
        assert "Fix the bug in utils.py" in user_msg.content
        assert FORK_BOILERPLATE_TAG in user_msg.content

    def test_build_fork_child_messages_no_tool_calls(self) -> None:
        """Assistant msg without tool_calls produces user msg with just directive."""
        assistant_msg = Message(role="assistant", content="Thinking...")

        result = build_fork_child_messages(assistant_msg, "Refactor module")

        assert len(result) == 2
        assert result[0] is assistant_msg
        user_msg = result[1]
        assert user_msg.role == "user"
        # No tool result placeholders, just the directive
        assert FORK_PLACEHOLDER_RESULT not in user_msg.content
        assert "Refactor module" in user_msg.content


# ---------------------------------------------------------------------------
# build_child_directive
# ---------------------------------------------------------------------------


class TestBuildChildDirective:
    def test_build_child_directive_format(self) -> None:
        directive = build_child_directive("Analyze security of auth.py")

        assert f"<{FORK_BOILERPLATE_TAG}>" in directive
        assert f"</{FORK_BOILERPLATE_TAG}>" in directive
        assert "You are a forked worker process" in directive
        assert "Do NOT spawn sub-agents or fork further" in directive
        assert f"{FORK_DIRECTIVE_PREFIX}Analyze security of auth.py" in directive
        assert "Scope:" in directive


# ---------------------------------------------------------------------------
# build_worktree_notice
# ---------------------------------------------------------------------------


class TestBuildWorktreeNotice:
    def test_build_worktree_notice(self) -> None:
        notice = build_worktree_notice("/home/user/project", "/tmp/worktree_abc")

        assert "/home/user/project" in notice
        assert "/tmp/worktree_abc" in notice
        assert "isolated git worktree" in notice
        assert "translate them to your worktree root" in notice


# ---------------------------------------------------------------------------
# is_in_fork_child (anti-recursion)
# ---------------------------------------------------------------------------


class TestIsInForkChild:
    def test_is_in_fork_child_detects_tag(self) -> None:
        messages = [
            Message(role="system", content="You are an agent."),
            Message(role="user", content=f"<{FORK_BOILERPLATE_TAG}>\ndo something\n</{FORK_BOILERPLATE_TAG}>"),
        ]
        assert is_in_fork_child(messages) is True

    def test_is_in_fork_child_no_tag(self) -> None:
        messages = [
            Message(role="system", content="You are an agent."),
            Message(role="user", content="Please analyze the code."),
            Message(role="assistant", content="Sure, let me look."),
        ]
        assert is_in_fork_child(messages) is False

    def test_is_in_fork_child_empty_messages(self) -> None:
        assert is_in_fork_child([]) is False

    def test_is_in_fork_child_ignores_assistant_role(self) -> None:
        """Tag in assistant message should NOT trigger detection."""
        messages = [
            Message(role="assistant", content=f"<{FORK_BOILERPLATE_TAG}>stuff</{FORK_BOILERPLATE_TAG}>"),
        ]
        assert is_in_fork_child(messages) is False
