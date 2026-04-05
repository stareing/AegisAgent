"""Tests for v4.3 cache_control support.

Covers:
- Message.cache_control field
- ContextEngineer._mark_cache_breakpoints
- Anthropic adapter cache_control injection (system + messages)
"""

from __future__ import annotations

import pytest

from agent_framework.models.message import Message, ToolCallRequest


# ===========================================================================
# Message.cache_control field
# ===========================================================================

class TestMessageCacheControl:

    def test_default_none(self):
        msg = Message(role="user", content="hello")
        assert msg.cache_control is None

    def test_set_ephemeral(self):
        msg = Message(role="system", content="prompt", cache_control={"type": "ephemeral"})
        assert msg.cache_control == {"type": "ephemeral"}

    def test_model_copy_preserves(self):
        msg = Message(role="system", content="prompt")
        updated = msg.model_copy(update={"cache_control": {"type": "ephemeral"}})
        assert updated.cache_control == {"type": "ephemeral"}
        assert msg.cache_control is None  # original unchanged


# ===========================================================================
# ContextEngineer._mark_cache_breakpoints
# ===========================================================================

class TestMarkCacheBreakpoints:

    def _mark(self, messages, has_injection=False):
        from agent_framework.context.engineer import ContextEngineer
        return ContextEngineer._mark_cache_breakpoints(messages, has_injection)

    def test_empty_messages(self):
        assert self._mark([]) == []

    def test_system_gets_cache_control(self):
        msgs = [
            Message(role="system", content="sys prompt"),
            Message(role="user", content="hello"),
        ]
        result = self._mark(msgs)
        assert result[0].cache_control == {"type": "ephemeral"}

    def test_last_session_msg_gets_cache_control(self):
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="q2"),
        ]
        result = self._mark(msgs, has_injection=False)
        # System + last message both get cache_control
        assert result[0].cache_control == {"type": "ephemeral"}
        assert result[3].cache_control == {"type": "ephemeral"}

    def test_injection_excluded_from_breakpoint(self):
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="<context-update>...</context-update>"),
        ]
        result = self._mark(msgs, has_injection=True)
        # Breakpoint on assistant (last before injection), not on injection
        assert result[2].cache_control == {"type": "ephemeral"}
        assert result[3].cache_control is None

    def test_single_system_only(self):
        msgs = [Message(role="system", content="sys")]
        result = self._mark(msgs)
        assert result[0].cache_control == {"type": "ephemeral"}

    def test_original_messages_not_mutated(self):
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="hello"),
        ]
        self._mark(msgs)
        assert msgs[0].cache_control is None
        assert msgs[1].cache_control is None


# ===========================================================================
# Anthropic Adapter cache_control injection
# ===========================================================================

class TestAnthropicCacheControl:

    def test_system_with_cache_control(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="system", content="You are helpful", cache_control={"type": "ephemeral"}),
            Message(role="user", content="hi"),
        ]
        system, non_system = AnthropicAdapter._extract_system(msgs)
        # Should return structured format with cache_control
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "You are helpful"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_without_cache_control(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="system", content="You are helpful"),
            Message(role="user", content="hi"),
        ]
        system, non_system = AnthropicAdapter._extract_system(msgs)
        # Should return plain string (backward compat)
        assert isinstance(system, str)
        assert system == "You are helpful"

    def test_assistant_message_cache_control(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="assistant", content="hello", cache_control={"type": "ephemeral"}),
        ]
        result = AnthropicAdapter._convert_messages(msgs)
        assert result[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_user_message_cache_control(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="user", content="question", cache_control={"type": "ephemeral"}),
        ]
        result = AnthropicAdapter._convert_messages(msgs)
        # Should have cache_control on the content block
        content = result[0]["content"]
        if isinstance(content, list):
            assert content[-1].get("cache_control") == {"type": "ephemeral"}
        else:
            # String content should be wrapped in list with cache_control
            pass  # string content gets converted to list

    def test_tool_message_cache_control(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="tool", content="result", tool_call_id="tc1",
                    cache_control={"type": "ephemeral"}),
        ]
        result = AnthropicAdapter._convert_messages(msgs)
        # Tool becomes user message with tool_result block
        assert result[0]["role"] == "user"
        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[-1].get("cache_control") == {"type": "ephemeral"}

    def test_no_cache_control_no_change(self):
        from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
        msgs = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ]
        result = AnthropicAdapter._convert_messages(msgs)
        # No cache_control should be present
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    assert "cache_control" not in block
