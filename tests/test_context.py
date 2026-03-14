"""Strict unit tests for context layer.

Covers:
- ToolTransactionGroup model
- ContextSourceProvider (system core, skill addon, memory block, session groups)
- ContextBuilder (5-slot assembly, trimming, spawn seed, token budget)
- ContextCompressor (sliding window, tool result summary)
- ContextEngineer (orchestration)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import CompressionStrategy, ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.models.agent import AgentConfig, AgentState, AgentStatus, Skill
from agent_framework.models.context import ContextStats
from agent_framework.models.memory import MemoryKind, MemoryRecord
from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.session import SessionState


# =====================================================================
# ToolTransactionGroup
# =====================================================================


class TestToolTransactionGroup:
    def test_defaults(self):
        g = ToolTransactionGroup()
        assert g.group_type == "PLAIN_MESSAGES"
        assert g.messages == []
        assert g.token_estimate == 0
        assert g.protected is False

    def test_with_messages(self):
        msgs = [Message(role="user", content="hi")]
        g = ToolTransactionGroup(
            group_id="g1",
            group_type="TOOL_BATCH",
            messages=msgs,
            token_estimate=10,
            protected=True,
        )
        assert g.group_id == "g1"
        assert g.group_type == "TOOL_BATCH"
        assert len(g.messages) == 1
        assert g.protected is True


# =====================================================================
# ContextSourceProvider
# =====================================================================


class TestContextSourceProvider:
    def setup_method(self):
        self.provider = ContextSourceProvider()

    def test_collect_system_core_basic(self):
        config = AgentConfig(system_prompt="You are helpful.")
        result = self.provider.collect_system_core(config)
        assert "You are helpful." in result

    def test_collect_system_core_with_runtime_info(self):
        config = AgentConfig(system_prompt="base")
        result = self.provider.collect_system_core(config, {"model": "gpt-4", "version": "1.0"})
        assert "gpt-4" in result
        assert "runtime-environment" in result

    def test_collect_skill_addon_with_skill(self):
        skill = Skill(skill_id="s1", system_prompt_addon="Use math tools")
        result = self.provider.collect_skill_addon(skill)
        assert "Use math tools" in result
        assert "active-skill" in result

    def test_collect_skill_addon_none(self):
        assert self.provider.collect_skill_addon(None) is None

    def test_collect_skill_addon_empty_prompt(self):
        skill = Skill(skill_id="s1", system_prompt_addon="")
        assert self.provider.collect_skill_addon(skill) is None

    def test_collect_saved_memory_block_empty(self):
        assert self.provider.collect_saved_memory_block([]) is None

    def test_collect_saved_memory_block_formats(self):
        records = [
            MemoryRecord(memory_id="m1", title="Pref A", content="value A", is_pinned=True, tags=["tag1"]),
            MemoryRecord(memory_id="m2", title="Pref B", content="value B"),
        ]
        result = self.provider.collect_saved_memory_block(records)
        assert "saved-memories" in result
        assert 'pinned="true"' in result
        assert "Pref A" in result
        assert "tag1" in result
        assert "Pref B" in result

    def test_collect_session_groups_plain(self):
        session = SessionState()
        session.append_message(Message(role="user", content="hello"))
        session.append_message(Message(role="assistant", content="hi"))
        groups = self.provider.collect_session_groups(session)
        assert len(groups) == 2
        assert groups[0].group_type == "PLAIN_MESSAGES"

    def test_collect_session_groups_tool_batch(self):
        session = SessionState()
        session.append_message(Message(
            role="assistant",
            content="Let me search",
            tool_calls=[ToolCallRequest(id="tc1", function_name="search", arguments={"q": "test"})],
        ))
        session.append_message(Message(role="tool", content="result", tool_call_id="tc1", name="search"))
        groups = self.provider.collect_session_groups(session)
        assert len(groups) == 1
        assert groups[0].group_type == "TOOL_BATCH"
        assert len(groups[0].messages) == 2

    def test_collect_session_groups_spawn_batch(self):
        session = SessionState()
        session.append_message(Message(
            role="assistant",
            content="Spawning",
            tool_calls=[ToolCallRequest(id="tc1", function_name="spawn_agent", arguments={"task": "sub"})],
        ))
        session.append_message(Message(role="tool", content="done", tool_call_id="tc1", name="spawn_agent"))
        groups = self.provider.collect_session_groups(session)
        assert groups[0].group_type == "SUBAGENT_BATCH"

    def test_collect_current_input(self):
        msg = self.provider.collect_current_input("what is 2+2?")
        assert msg.role == "user"
        assert msg.content == "what is 2+2?"

    def test_collect_session_groups_empty(self):
        session = SessionState()
        groups = self.provider.collect_session_groups(session)
        assert groups == []


# =====================================================================
# ContextBuilder
# =====================================================================


class TestContextBuilder:
    def setup_method(self):
        # Use a simple counter: 1 token per character
        self.builder = ContextBuilder(
            token_counter=lambda msgs: sum(len(m.content or "") for m in msgs),
            max_context_tokens=200,
            reserve_for_output=50,
        )

    def test_build_context_basic(self):
        result = self.builder.build_context(
            system_core="system",
            skill_addon=None,
            memory_block=None,
            session_groups=[],
            current_input=Message(role="user", content="hello"),
        )
        assert len(result) == 2  # system + user
        assert result[0].role == "system"
        assert result[-1].role == "user"
        assert "system" in result[0].content

    def test_build_context_with_skill_and_memory(self):
        result = self.builder.build_context(
            system_core="core",
            skill_addon="skill info",
            memory_block="memory info",
            session_groups=[],
            current_input=Message(role="user", content="hi"),
        )
        system_content = result[0].content
        assert "core" in system_content
        assert "skill info" in system_content
        assert "memory info" in system_content

    def test_build_context_with_session_groups(self):
        groups = [
            ToolTransactionGroup(
                group_id="g1",
                messages=[Message(role="user", content="prev")],
            )
        ]
        result = self.builder.build_context(
            system_core="sys",
            skill_addon=None,
            memory_block=None,
            session_groups=groups,
            current_input=Message(role="user", content="now"),
        )
        assert len(result) == 3  # system + session msg + current
        assert result[1].content == "prev"

    def test_trimming_oldest_first(self):
        # Create groups that exceed budget
        groups = []
        for i in range(20):
            groups.append(ToolTransactionGroup(
                group_id=f"g{i}",
                messages=[Message(role="user", content="x" * 10)],
            ))
        result = self.builder.build_context(
            system_core="s",
            skill_addon=None,
            memory_block=None,
            session_groups=groups,
            current_input=Message(role="user", content="q"),
        )
        # Should have trimmed older groups
        total_tokens = sum(len(m.content or "") for m in result)
        assert total_tokens <= 150  # budget = 200 - 50

    def test_protected_group_not_trimmed(self):
        groups = [
            ToolTransactionGroup(
                group_id="protected",
                messages=[Message(role="user", content="x" * 100)],
                protected=True,
            ),
            ToolTransactionGroup(
                group_id="normal",
                messages=[Message(role="user", content="y" * 10)],
            ),
        ]
        result = self.builder.build_context(
            system_core="s",
            skill_addon=None,
            memory_block=None,
            session_groups=groups,
            current_input=Message(role="user", content="q"),
        )
        # Protected group should remain even if over budget
        contents = [m.content for m in result]
        assert any("x" * 100 in (c or "") for c in contents)

    def test_set_token_budget(self):
        self.builder.set_token_budget(500, 100)
        assert self.builder._max_tokens == 500
        assert self.builder._reserve_for_output == 100

    def test_rough_count(self):
        msgs = [Message(role="user", content="hello world")]  # 11 chars -> ~2 tokens
        count = ContextBuilder._rough_count(msgs)
        assert count >= 1

    def test_rough_count_empty_content(self):
        msgs = [Message(role="user")]
        count = ContextBuilder._rough_count(msgs)
        assert count >= 1  # min 1

    def test_build_spawn_seed_basic(self):
        parent_msgs = [
            Message(role="user", content="aaaa"),
            Message(role="assistant", content="bbbb"),
        ]
        seed = self.builder.build_spawn_seed(parent_msgs, "task query", token_budget=50)
        assert seed[-1].role == "user"
        assert seed[-1].content == "task query"

    def test_build_spawn_seed_respects_budget(self):
        parent_msgs = [Message(role="user", content="x" * 100) for _ in range(10)]
        seed = self.builder.build_spawn_seed(parent_msgs, "q", token_budget=20)
        # Should only include the query since parent msgs are too large
        assert len(seed) >= 1
        assert seed[-1].content == "q"

    def test_build_spawn_seed_empty_parent(self):
        seed = self.builder.build_spawn_seed([], "query", token_budget=100)
        assert len(seed) == 1
        assert seed[0].content == "query"

    def test_allocate_slot_budgets(self):
        budgets = self.builder._allocate_slot_budgets()
        assert "system_core" in budgets
        assert "session_history" in budgets
        total = sum(budgets.values())
        assert total <= 150  # budget = 200 - 50


# =====================================================================
# ContextCompressor
# =====================================================================


class TestContextCompressor:
    def setup_method(self):
        self.counter = lambda msgs: sum(len(m.content or "") for m in msgs)

    def test_sliding_window_fits(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.SLIDING_WINDOW,
            token_counter=self.counter,
        )
        groups = [
            ToolTransactionGroup(messages=[Message(role="user", content="short")], token_estimate=5),
        ]
        result = comp.compress_groups(groups, target_tokens=100)
        assert len(result) == 1

    def test_sliding_window_trims_oldest(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.SLIDING_WINDOW,
            token_counter=self.counter,
        )
        groups = [
            ToolTransactionGroup(messages=[Message(role="user", content="a" * 50)], token_estimate=50),
            ToolTransactionGroup(messages=[Message(role="user", content="b" * 50)], token_estimate=50),
            ToolTransactionGroup(messages=[Message(role="user", content="c" * 50)], token_estimate=50),
        ]
        result = comp.compress_groups(groups, target_tokens=60)
        # Should keep only the most recent that fit
        assert len(result) <= 2

    def test_tool_result_summary_truncates(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.TOOL_RESULT_SUMMARY,
            token_counter=self.counter,
        )
        long_output = "x" * 500
        groups = [
            ToolTransactionGroup(
                group_type="TOOL_BATCH",
                messages=[
                    Message(role="assistant", content="calling tool"),
                    Message(role="tool", content=long_output),
                ],
                token_estimate=500,
            ),
        ]
        result = comp.compress_groups(groups, target_tokens=300)
        # Tool result should be truncated
        tool_msg = [m for g in result for m in g.messages if m.role == "tool"]
        if tool_msg:
            assert len(tool_msg[0].content) < len(long_output)
            assert "[truncated]" in tool_msg[0].content

    def test_under_budget_no_compression(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.TOOL_RESULT_SUMMARY,
            token_counter=self.counter,
        )
        groups = [
            ToolTransactionGroup(messages=[Message(role="user", content="small")], token_estimate=5),
        ]
        result = comp.compress_groups(groups, target_tokens=100)
        assert len(result) == 1

    def test_llm_summarize_falls_back_to_sliding_window(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.LLM_SUMMARIZE,
            token_counter=self.counter,
        )
        groups = [
            ToolTransactionGroup(messages=[Message(role="user", content="a" * 100)], token_estimate=100),
            ToolTransactionGroup(messages=[Message(role="user", content="b" * 100)], token_estimate=100),
        ]
        result = comp.compress_groups(groups, target_tokens=110)
        assert len(result) <= 2

    def test_protected_group_kept_in_sliding_window(self):
        comp = ContextCompressor(
            strategy=CompressionStrategy.SLIDING_WINDOW,
            token_counter=self.counter,
        )
        groups = [
            ToolTransactionGroup(
                messages=[Message(role="user", content="a" * 50)],
                token_estimate=50,
                protected=True,
            ),
            ToolTransactionGroup(
                messages=[Message(role="user", content="b" * 50)],
                token_estimate=50,
            ),
        ]
        result = comp.compress_groups(groups, target_tokens=60)
        # Protected group should still be present
        assert any(g.protected for g in result)


# =====================================================================
# ContextEngineer
# =====================================================================


class TestContextEngineer:
    def test_prepare_context_basic(self):
        engineer = ContextEngineer()
        agent_config = AgentConfig(system_prompt="Be helpful")
        session = SessionState()
        state = AgentState(run_id="r1", task="greet")

        materials = {
            "agent_config": agent_config,
            "session_state": session,
            "memories": [],
            "task": "greet",
            "active_skill": None,
        }
        messages = engineer.prepare_context_for_llm(state, materials)
        assert len(messages) >= 2
        assert messages[0].role == "system"
        assert messages[-1].role == "user"

    def test_prepare_context_with_memories(self):
        engineer = ContextEngineer()
        agent_config = AgentConfig(system_prompt="sys")
        session = SessionState()
        state = AgentState(run_id="r1", task="test")

        memories = [
            MemoryRecord(memory_id="m1", title="Pref", content="val", is_pinned=True),
        ]
        materials = {
            "agent_config": agent_config,
            "session_state": session,
            "memories": memories,
            "task": "test",
        }
        messages = engineer.prepare_context_for_llm(state, materials)
        system_content = messages[0].content
        assert "saved-memories" in system_content
        assert "Pref" in system_content

    def test_set_skill_context(self):
        engineer = ContextEngineer()
        engineer.set_skill_context("custom skill prompt")
        agent_config = AgentConfig(system_prompt="base")
        session = SessionState()
        state = AgentState(run_id="r1", task="test")

        materials = {
            "agent_config": agent_config,
            "session_state": session,
            "task": "test",
        }
        messages = engineer.prepare_context_for_llm(state, materials)
        assert "custom skill prompt" in messages[0].content

    def test_report_context_stats(self):
        engineer = ContextEngineer()
        agent_config = AgentConfig(system_prompt="sys")
        session = SessionState()
        state = AgentState(run_id="r1", task="test")
        materials = {
            "agent_config": agent_config,
            "session_state": session,
            "task": "test",
        }
        engineer.prepare_context_for_llm(state, materials)
        stats = engineer.report_context_stats()
        assert isinstance(stats, ContextStats)
        assert stats.total_tokens > 0

    def test_build_spawn_seed_delegates(self):
        engineer = ContextEngineer()
        seed = engineer.build_spawn_seed([], "query", token_budget=100)
        assert len(seed) >= 1
        assert seed[-1].content == "query"


# =====================================================================
# FrozenPromptPrefix / PromptPrefixManager
# =====================================================================


class TestFrozenPromptPrefix:

    def test_prefix_frozen(self):
        from agent_framework.models.context import FrozenPromptPrefix
        prefix = FrozenPromptPrefix(
            messages=[Message(role="system", content="hello")],
            prefix_hash="abc",
        )
        with pytest.raises(Exception):
            prefix.prefix_hash = "changed"

    def test_prefix_has_system_message(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        prefix = mgr.get_or_create("You are helpful.")
        assert len(prefix.messages) == 1
        assert prefix.messages[0].role == "system"
        assert "helpful" in prefix.messages[0].content

    def test_prefix_cached_on_same_input(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        p1 = mgr.get_or_create("System prompt A")
        p2 = mgr.get_or_create("System prompt A")
        assert p1.prefix_id == p2.prefix_id
        assert p1.prefix_hash == p2.prefix_hash

    def test_prefix_rotated_on_different_input(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        p1 = mgr.get_or_create("System prompt A")
        p2 = mgr.get_or_create("System prompt B")
        assert p1.prefix_hash != p2.prefix_hash
        assert p2.prefix_epoch == 2

    def test_prefix_includes_skill_addon(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        prefix = mgr.get_or_create("Base prompt", "Skill instructions")
        assert prefix.includes_skill_addon is True
        assert "Skill instructions" in prefix.messages[0].content

    def test_prefix_without_skill_addon(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        prefix = mgr.get_or_create("Base prompt")
        assert prefix.includes_skill_addon is False

    def test_skill_change_triggers_rotation(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        mgr.get_or_create("Base", "Skill A")
        assert mgr.should_rotate("Base", "Skill B") is True
        assert mgr.should_rotate("Base", "Skill A") is False

    def test_same_input_deterministic_hash(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        h1 = PromptPrefixManager._compute_hash("core", "addon")
        h2 = PromptPrefixManager._compute_hash("core", "addon")
        assert h1 == h2

    def test_invalidate_forces_rotation(self):
        from agent_framework.context.prefix_manager import PromptPrefixManager
        mgr = PromptPrefixManager()
        mgr.get_or_create("prompt")
        mgr.invalidate()
        assert mgr.current_prefix is None
        assert mgr.should_rotate("prompt") is True

    def test_context_stats_reports_prefix_reuse(self):
        engineer = ContextEngineer()
        config = AgentConfig(system_prompt="stable prompt")
        session = SessionState()
        state = AgentState(run_id="r1", task="test")
        materials = {"agent_config": config, "session_state": session, "task": "test"}

        # First call — builds prefix
        engineer.prepare_context_for_llm(state, materials)
        stats1 = engineer.report_context_stats()

        # Second call — should reuse prefix
        engineer.prepare_context_for_llm(state, materials)
        stats2 = engineer.report_context_stats()
        assert stats2.prefix_reused is True

    def test_prefix_not_compressed(self):
        """Frozen prefix must survive compression — only suffix is trimmed."""
        engineer = ContextEngineer(
            builder=ContextBuilder(max_context_tokens=200, reserve_for_output=20),
        )
        config = AgentConfig(system_prompt="X" * 100)
        session = SessionState()
        # Add many messages to force compression
        for i in range(20):
            session.append_message(Message(role="user", content=f"msg {i} " + "Y" * 50))
            session.append_message(Message(role="assistant", content=f"reply {i} " + "Z" * 50))
        state = AgentState(run_id="r1", task="test")
        materials = {"agent_config": config, "session_state": session, "task": "q"}
        messages = engineer.prepare_context_for_llm(state, materials)
        # System message (prefix) must be first and contain full system prompt
        assert messages[0].role == "system"
        assert "X" * 100 in messages[0].content
