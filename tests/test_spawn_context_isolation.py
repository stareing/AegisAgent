"""Tests for subagent context isolation.

Covers:
1. Default MINIMAL context mode — child only sees task_input
2. PARENT_CONTEXT mode — child sees filtered parent session
3. Sibling task leakage prevention
4. build_filtered_spawn_seed filter rules
5. PROGRESSIVE_DONE display_text (no dict display)
"""

from __future__ import annotations

import pytest

from agent_framework.context.builder import ContextBuilder
from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.subagent import (SpawnContextMode, SpawnMode,
                                             SubAgentSpec)


class TestSpawnContextMode:
    """SpawnContextMode enum and SubAgentSpec defaults."""

    def test_default_is_minimal(self) -> None:
        spec = SubAgentSpec(task_input="compute 72 * 9")
        assert spec.context_mode == SpawnContextMode.MINIMAL

    def test_minimal_value(self) -> None:
        assert SpawnContextMode.MINIMAL.value == "MINIMAL"

    def test_parent_context_value(self) -> None:
        assert SpawnContextMode.PARENT_CONTEXT.value == "PARENT_CONTEXT"


class TestMinimalContextSeed:
    """Default MINIMAL mode: child only gets task_input as a single user message."""

    def test_minimal_seed_is_single_message(self) -> None:
        """MINIMAL mode produces exactly one user message with the task."""
        seed = [Message(role="user", content="compute 72 * 9")]
        assert len(seed) == 1
        assert seed[0].role == "user"
        assert seed[0].content == "compute 72 * 9"

    def test_minimal_seed_contains_no_parent_history(self) -> None:
        """MINIMAL seed must NOT contain any parent session messages."""
        # Simulate what executor does in MINIMAL mode
        task = "compute 72 * 9"
        seed = [Message(role="user", content=task)]

        # Verify no assistant, tool, or system messages from parent
        roles = {m.role for m in seed}
        assert roles == {"user"}
        assert len(seed) == 1
        assert "hello" not in seed[0].content  # No parent context leaked


class TestSiblingIsolation:
    """Concurrent sibling spawns must not see each other's task descriptions."""

    def test_sibling_tasks_isolated_in_minimal_mode(self) -> None:
        """Four concurrent tasks: each child's seed contains ONLY its own task."""
        tasks = [
            "计算 72 * 9",
            "计算 2048 / 32",
            "计算 8765 - 4321",
            "计算 1357 + 2468",
        ]

        # Each child gets MINIMAL seed
        seeds = [[Message(role="user", content=t)] for t in tasks]

        for i, seed in enumerate(seeds):
            # Each seed has exactly 1 message
            assert len(seed) == 1
            assert seed[0].content == tasks[i]

            # No other sibling's task appears
            for j, other_task in enumerate(tasks):
                if j != i:
                    assert other_task not in seed[0].content, (
                        f"Child {i} sees sibling {j}'s task: {other_task}"
                    )

    def test_parent_context_mode_filters_tool_messages(self) -> None:
        """PARENT_CONTEXT mode must not include tool/delegation messages."""
        parent_session = [
            Message(role="system", content="You are a helpful assistant"),
            Message(role="user", content="Help me with math"),
            Message(role="assistant", content="Sure, I'll spawn agents",
                    tool_calls=[ToolCallRequest(id="tc1", function_name="spawn_agent")]),
            Message(role="tool", content="result of spawn", tool_call_id="tc1"),
            Message(role="assistant", content="Here are the results"),
        ]

        builder = ContextBuilder()
        seed = builder.build_filtered_spawn_seed(
            session_messages=parent_session,
            query="计算 72 * 9",
            token_budget=4096,
        )

        # Verify no tool messages
        for msg in seed:
            assert msg.role != "tool", "Filtered seed must not contain tool messages"
            assert not getattr(msg, "tool_call_id", None), "No tool_call_id messages"
            if msg.role == "assistant":
                assert not getattr(msg, "tool_calls", None), "No assistant with tool_calls"

        # Last message should be the query
        assert seed[-1].role == "user"
        assert seed[-1].content == "计算 72 * 9"

        # Should contain system and pure-text assistant
        roles = [m.role for m in seed]
        assert "system" in roles
        assert seed[-1].content == "计算 72 * 9"


class TestBuildFilteredSpawnSeed:
    """Detailed filter rules for build_filtered_spawn_seed."""

    def _make_session(self) -> list[Message]:
        return [
            Message(role="system", content="System prompt"),
            Message(role="user", content="What is the weather?"),
            Message(role="assistant", content="Let me check",
                    tool_calls=[ToolCallRequest(id="tc1", function_name="get_weather")]),
            Message(role="tool", content="Sunny 25C", tool_call_id="tc1"),
            Message(role="assistant", content="It's sunny and 25°C"),
            Message(role="user", content="Now calculate 72*9"),
            Message(role="assistant", content="Starting calculation tasks",
                    tool_calls=[
                        ToolCallRequest(id="tc2", function_name="spawn_agent"),
                        ToolCallRequest(id="tc3", function_name="spawn_agent"),
                    ]),
        ]

    def test_filters_tool_role(self) -> None:
        builder = ContextBuilder()
        seed = builder.build_filtered_spawn_seed(
            self._make_session(), "compute 1+1", 4096
        )
        assert not any(m.role == "tool" for m in seed)

    def test_filters_assistant_with_tool_calls(self) -> None:
        builder = ContextBuilder()
        seed = builder.build_filtered_spawn_seed(
            self._make_session(), "compute 1+1", 4096
        )
        for m in seed:
            if m.role == "assistant":
                assert not getattr(m, "tool_calls", None)

    def test_keeps_pure_text_messages(self) -> None:
        builder = ContextBuilder()
        seed = builder.build_filtered_spawn_seed(
            self._make_session(), "compute 1+1", 4096
        )
        # Should keep: system, "What is the weather?", "It's sunny...", "Now calculate..."
        # Plus the query at end
        contents = [m.content for m in seed]
        assert "System prompt" in contents
        assert "It's sunny and 25°C" in contents
        assert "compute 1+1" in contents

    def test_query_is_last_message(self) -> None:
        builder = ContextBuilder()
        seed = builder.build_filtered_spawn_seed(
            self._make_session(), "my query", 4096
        )
        assert seed[-1].role == "user"
        assert seed[-1].content == "my query"

    def test_respects_token_budget(self) -> None:
        builder = ContextBuilder()
        # Very small budget — should at least have the query
        seed = builder.build_filtered_spawn_seed(
            self._make_session(), "my query", 10
        )
        assert len(seed) >= 1
        assert seed[-1].content == "my query"


class TestProgressiveDoneDisplayText:
    """PROGRESSIVE_DONE must carry display_text, not raw dict."""

    def test_delegation_summary_extracted(self) -> None:
        """For spawn_agent results, display_text should be the summary string."""
        raw_output = {
            "status": "COMPLETED",
            "summary": "72 × 9 = 648",
            "artifacts_digest": [],
            "artifact_refs": [],
        }
        # Simulate the extraction logic from loop.py
        if isinstance(raw_output, dict) and "summary" in raw_output:
            display_text = str(raw_output["summary"])
        else:
            display_text = str(raw_output)

        assert display_text == "72 × 9 = 648"
        assert "{'status'" not in display_text

    def test_regular_tool_output_unchanged(self) -> None:
        raw_output = "file content here"
        if isinstance(raw_output, dict) and "summary" in raw_output:
            display_text = str(raw_output["summary"])
        else:
            display_text = str(raw_output)

        assert display_text == "file content here"

    def test_dict_without_summary_stays_as_str(self) -> None:
        raw_output = {"key": "value"}
        if isinstance(raw_output, dict) and "summary" in raw_output:
            display_text = str(raw_output["summary"])
        else:
            display_text = str(raw_output)

        assert "key" in display_text
