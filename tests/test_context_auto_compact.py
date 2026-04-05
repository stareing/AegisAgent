"""Tests for context auto-compaction, SNIP strategy, and PostCompactRestorer.

Covers:
- PostCompactRestorer with files and active skill
- SNIP compression strategy on long tool outputs
- SNIP preserves short outputs unchanged
- Auto-compact threshold detection in ContextEngineer
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.context.post_compact_restorer import PostCompactRestorer
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    AgentStatus,
    Skill,
)
from agent_framework.models.message import Message
from agent_framework.models.session import SessionState


# =====================================================================
# PostCompactRestorer
# =====================================================================


class TestPostCompactRestorer:
    def test_restore_with_files_and_skill(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="user", content="hello")]
        skill = Skill(
            skill_id="test-skill",
            name="Test",
            system_prompt_addon="Always use JSON output.",
        )
        files = ["/src/main.py", "/src/utils.py", "/src/config.py"]

        result = restorer.restore(messages, recently_accessed_files=files, active_skill=skill)

        assert len(result) == 2  # original + restoration message
        restoration = result[-1]
        assert restoration.role == "user"
        assert "<post-compaction-context>" in restoration.content
        assert "<recently-accessed-files>" in restoration.content
        assert "/src/main.py" in restoration.content
        assert "/src/utils.py" in restoration.content
        assert "<active-skill-context" in restoration.content
        assert "Always use JSON output." in restoration.content

    def test_restore_limits_to_five_files(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="user", content="hi")]
        files = [f"/src/file_{i}.py" for i in range(10)]

        result = restorer.restore(messages, recently_accessed_files=files)

        restoration = result[-1].content
        # Only first 5 files should be present
        assert "/src/file_0.py" in restoration
        assert "/src/file_4.py" in restoration
        assert "/src/file_5.py" not in restoration

    def test_restore_with_only_skill(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="assistant", content="done")]
        skill = Skill(skill_id="s1", system_prompt_addon="Be concise.")

        result = restorer.restore(messages, active_skill=skill)

        assert len(result) == 2
        assert "<active-skill-context" in result[-1].content
        assert "Be concise." in result[-1].content
        assert "<recently-accessed-files>" not in result[-1].content

    def test_restore_with_only_files(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="user", content="x")]
        files = ["/a.py"]

        result = restorer.restore(messages, recently_accessed_files=files)

        assert len(result) == 2
        assert "<recently-accessed-files>" in result[-1].content
        assert "<active-skill-context" not in result[-1].content

    def test_restore_noop_when_nothing_to_restore(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="user", content="x")]

        result = restorer.restore(messages)
        assert result is messages  # identity — no copy

    def test_restore_noop_with_empty_skill_addon(self):
        restorer = PostCompactRestorer()
        messages = [Message(role="user", content="x")]
        skill = Skill(skill_id="s1", system_prompt_addon="")

        result = restorer.restore(messages, active_skill=skill)
        assert result is messages


# =====================================================================
# SNIP Compression Strategy
# =====================================================================


class TestSnipStrategy:
    def _make_tool_group(self, content: str, group_id: str = "g1") -> ToolTransactionGroup:
        msg = Message(role="tool", content=content, tool_call_id="tc1", name="read_file")
        return ToolTransactionGroup(
            group_id=group_id,
            group_type="TOOL_BATCH",
            messages=[msg],
            token_estimate=len(content) // 4,
        )

    @pytest.mark.asyncio
    async def test_snip_long_tool_output(self):
        long_content = "x" * 1000
        group = self._make_tool_group(long_content)
        compressor = ContextCompressor(strategy="SNIP")

        result = await compressor.compress_groups_async([group], target_tokens=50)

        assert len(result) == 1
        snipped = result[0].messages[0].content
        assert "[content snipped: 1000 chars]" in snipped
        # Head preserved
        assert snipped.startswith("x" * 200)
        # Tail preserved
        assert snipped.endswith("x" * 100)
        # Shorter than original
        assert len(snipped) < len(long_content)

    @pytest.mark.asyncio
    async def test_snip_preserves_short_output(self):
        short_content = "short result"
        group = self._make_tool_group(short_content)
        compressor = ContextCompressor(strategy="SNIP")

        result = await compressor.compress_groups_async([group], target_tokens=10)

        assert result[0].messages[0].content == short_content

    @pytest.mark.asyncio
    async def test_snip_preserves_user_and_assistant_messages(self):
        long_text = "y" * 1000
        user_msg = Message(role="user", content=long_text)
        assistant_msg = Message(role="assistant", content=long_text)
        tool_msg = Message(role="tool", content=long_text, tool_call_id="tc1", name="run")
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="TOOL_BATCH",
            messages=[user_msg, assistant_msg, tool_msg],
            token_estimate=750,
        )
        compressor = ContextCompressor(strategy="SNIP")

        result = await compressor.compress_groups_async([group], target_tokens=100)

        msgs = result[0].messages
        # User and assistant are untouched
        assert msgs[0].content == long_text
        assert msgs[1].content == long_text
        # Tool message is snipped
        assert "[content snipped: 1000 chars]" in msgs[2].content

    @pytest.mark.asyncio
    async def test_snip_boundary_exactly_500(self):
        """Content at exactly the threshold (500 chars) should NOT be snipped."""
        content_500 = "a" * 500
        group = self._make_tool_group(content_500)
        compressor = ContextCompressor(strategy="SNIP")

        result = await compressor.compress_groups_async([group], target_tokens=50)

        assert result[0].messages[0].content == content_500

    @pytest.mark.asyncio
    async def test_snip_boundary_501(self):
        """Content at 501 chars should be snipped."""
        content_501 = "b" * 501
        group = self._make_tool_group(content_501)
        compressor = ContextCompressor(strategy="SNIP")

        result = await compressor.compress_groups_async([group], target_tokens=50)

        assert "[content snipped: 501 chars]" in result[0].messages[0].content

    @pytest.mark.asyncio
    async def test_snip_no_compress_under_budget(self):
        """When total tokens are under target, SNIP still applies (it doesn't
        short-circuit like other strategies) because groups pass through
        the snip method regardless."""
        long_content = "z" * 800
        group = self._make_tool_group(long_content)
        compressor = ContextCompressor(strategy="SNIP")

        # Target well above current tokens — but SNIP is triggered before
        # the budget check because the strategy dispatch runs after the
        # under-budget early return. So for under-budget, groups are returned as-is.
        result = await compressor.compress_groups_async([group], target_tokens=999999)

        # Under budget → returned as-is (no snipping)
        assert result[0].messages[0].content == long_content


# =====================================================================
# Auto-compaction threshold in ContextEngineer
# =====================================================================


class TestAutoCompactThreshold:
    @pytest.mark.asyncio
    async def test_auto_compact_triggers_when_over_threshold(self):
        """When token count exceeds threshold ratio of budget, auto-compact fires."""
        from agent_framework.context.builder import ContextBuilder

        builder = ContextBuilder()
        # Mock calculate_tokens to return controllable values
        call_count = {"n": 0}

        def mock_tokens(msgs):
            call_count["n"] += 1
            # First call: prefix tokens; subsequent calls return high count
            # to trigger auto-compaction
            return len(msgs) * 500

        builder.calculate_tokens = mock_tokens
        builder._max_tokens = 8192
        builder._reserve_for_output = 1024

        compressor = ContextCompressor(strategy="SUMMARIZATION")
        engineer = ContextEngineer(builder=builder, compressor=compressor)
        # Set a low threshold to ensure trigger
        engineer._auto_compact_threshold = 0.1

        agent_state = AgentState(
            run_id="run-1",
            task="test task",
            status=AgentStatus.RUNNING,
            iteration_count=1,
        )
        session_state = SessionState()
        session_state.messages = [
            Message(role="user", content="task"),
            Message(role="assistant", content="response"),
            Message(role="tool", content="x" * 1000, tool_call_id="t1", name="read"),
        ]
        agent_config = AgentConfig(
            agent_id="a1",
            name="test",
            system_prompt="You are a test agent.",
            max_iterations=10,
        )

        context_materials = {
            "agent_config": agent_config,
            "session_state": session_state,
            "memories": [],
            "task": "test task",
            "active_skill": None,
            "recently_accessed_files": ["/src/main.py", "/src/utils.py"],
        }

        # The test verifies that auto-compact path is exercised without error
        messages = await engineer.prepare_context_for_llm(agent_state, context_materials)
        assert len(messages) > 0
        # With auto-compact triggered and files provided, a restoration message
        # should be present
        restoration_msgs = [m for m in messages if m.content and "<post-compaction-context>" in m.content]
        assert len(restoration_msgs) == 1
        assert "/src/main.py" in restoration_msgs[0].content

    @pytest.mark.asyncio
    async def test_no_auto_compact_below_threshold(self):
        """When token count is below threshold, no auto-compact occurs."""
        from agent_framework.context.builder import ContextBuilder

        builder = ContextBuilder()
        # Return very low token counts
        builder.calculate_tokens = lambda msgs: len(msgs) * 2
        builder._max_tokens = 8192
        builder._reserve_for_output = 1024

        compressor = ContextCompressor(strategy="SUMMARIZATION")
        engineer = ContextEngineer(builder=builder, compressor=compressor)
        # Default threshold 0.7

        agent_state = AgentState(
            run_id="run-2",
            task="test",
            status=AgentStatus.RUNNING,
            iteration_count=1,
        )
        session_state = SessionState()
        session_state.messages = [Message(role="user", content="hi")]
        agent_config = AgentConfig(
            agent_id="a1",
            name="test",
            system_prompt="prompt",
            max_iterations=10,
        )

        context_materials = {
            "agent_config": agent_config,
            "session_state": session_state,
            "memories": [],
            "task": "test",
            "recently_accessed_files": ["/a.py"],
        }

        messages = await engineer.prepare_context_for_llm(agent_state, context_materials)
        # No restoration message should be present (no auto-compact triggered)
        restoration_msgs = [m for m in messages if m.content and "<post-compaction-context>" in m.content]
        assert len(restoration_msgs) == 0
