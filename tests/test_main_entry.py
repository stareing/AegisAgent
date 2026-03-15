from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_framework.main as main_module
from agent_framework.textual_cli import AegisAgentApp, _filter_entries
from agent_framework.terminal_runtime import (
    CommandPaletteEntry,
    ReplState,
    _execute_with_progressive,
    format_result,
)
from agent_framework.infra.config import FrameworkConfig
from agent_framework.models.agent import AgentRunResult, IterationResult
from agent_framework.models.message import Message, ModelResponse, TokenUsage
from agent_framework.models.stream import StreamEvent, StreamEventType


def test_main_delegates_to_cli_run(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_run(argv):
        called["argv"] = argv
        return 7

    monkeypatch.setattr(main_module, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        main_module.main(["--mock"])

    assert exc.value.code == 7
    assert called["argv"] == ["--mock"]


def test_textual_app_has_flush_updates() -> None:
    """AegisAgentApp must have _flush_updates for batched rendering."""
    assert hasattr(AegisAgentApp, "_flush_updates")


def test_format_result_can_omit_trace() -> None:
    result = AgentRunResult(
        success=True,
        final_answer="done",
        usage=TokenUsage(total_tokens=5),
        iterations_used=1,
        iteration_history=[
            IterationResult(
                iteration_index=0,
                model_response=ModelResponse(content="thinking", usage=TokenUsage(total_tokens=5)),
            )
        ],
    )

    compact = format_result(result, include_trace=False)
    detailed = format_result(result, include_trace=True)

    assert "执行轨迹" not in compact
    assert "执行轨迹" in detailed


def test_repl_state_tracks_token_estimate_incrementally() -> None:
    state = ReplState()
    result = AgentRunResult(
        success=True,
        final_answer="done",
        usage=TokenUsage(total_tokens=5),
        iterations_used=1,
        iteration_history=[],
    )

    state.append_turn("hello", result)

    expected = (
        ReplState._estimate_message_tokens(Message(role="user", content="hello"))
        + ReplState._estimate_message_tokens(Message(role="assistant", content="done"))
    )
    assert state.total_tokens_estimate == expected
    state.clear()
    assert state.total_tokens_estimate == 0


def test_filter_entries_caps_results() -> None:
    entries = [
        CommandPaletteEntry(
            command=f"/cmd{i}",
            title=f"cmd{i}",
            description="desc",
            category="test",
        )
        for i in range(80)
    ]

    filtered = _filter_entries(entries, "", [])

    assert len(filtered) == 40


def test_framework_config_default_temperature_is_one() -> None:
    cfg = FrameworkConfig()

    assert cfg.model.temperature == 1.0


@pytest.mark.asyncio
async def test_execute_with_progressive_suppresses_duplicate_tool_done(capsys: pytest.CaptureFixture[str]) -> None:
    result = AgentRunResult(
        success=True,
        final_answer="done",
        usage=TokenUsage(total_tokens=5),
        iterations_used=1,
        iteration_history=[],
    )

    class FakeFramework:
        async def run_stream(self, *args, **kwargs):
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                data={"tool_name": "spawn_agent", "tool_call_id": "tc_1", "arguments": {}},
            )
            yield StreamEvent(
                type=StreamEventType.SUBAGENT_START,
                data={"tool_call_id": "tc_1", "index": 1, "total": 1, "task_input": "demo"},
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_DONE,
                data={"tool_name": "spawn_agent", "tool_call_id": "tc_1", "success": True, "output": "ok"},
            )
            yield StreamEvent(
                type=StreamEventType.SUBAGENT_DONE,
                data={
                    "tool_call_id": "tc_1",
                    "tool_name": "spawn_agent",
                    "index": 1,
                    "total": 1,
                    "success": True,
                    "output": "done",
                },
            )
            yield StreamEvent(type=StreamEventType.DONE, data={"result": result})

    output = await _execute_with_progressive(FakeFramework(), None, ReplState(), "task")
    captured = capsys.readouterr().out

    assert "[ok]" not in captured
    assert "[subagent 1/1]" in captured
    assert "Agent 最终回复" in output


@pytest.mark.asyncio
async def test_execute_with_progressive_preserves_event_text_order(capsys: pytest.CaptureFixture[str]) -> None:
    result = AgentRunResult(
        success=True,
        final_answer="final answer",
        usage=TokenUsage(total_tokens=8),
        iterations_used=2,
        iteration_history=[],
    )

    class FakeFramework:
        async def run_stream(self, *args, **kwargs):
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                data={"tool_name": "spawn_agent", "tool_call_id": "tc_1", "arguments": {}},
            )
            yield StreamEvent(
                type=StreamEventType.SUBAGENT_START,
                data={"tool_call_id": "tc_1", "index": 1, "total": 2, "task_input": "task 1"},
            )
            yield StreamEvent(
                type=StreamEventType.SUBAGENT_DONE,
                data={
                    "tool_call_id": "tc_1",
                    "tool_name": "spawn_agent",
                    "index": 1,
                    "total": 2,
                    "success": True,
                    "output": "result 1",
                },
            )
            yield StreamEvent(
                type=StreamEventType.PROGRESSIVE_RESPONSE,
                data={"text": "mid response", "index": 1, "total": 2},
            )
            yield StreamEvent(type=StreamEventType.DONE, data={"result": result})

    import agent_framework.terminal_runtime as _rt
    orig = _rt._NO_COLOR
    _rt._NO_COLOR = True
    try:
        output = await _execute_with_progressive(FakeFramework(), None, ReplState(), "task")
    finally:
        _rt._NO_COLOR = orig
    captured = capsys.readouterr().out

    tool_pos = captured.index("[tool]")
    start_pos = captured.index("启动:")
    done_pos = captured.index("完成:")
    progressive_pos = captured.index("Agent [1/2]:")
    final_pos = output.index("Agent 最终回复")

    assert tool_pos < start_pos < done_pos < progressive_pos
    assert "final answer" in output
    assert final_pos >= 0


@pytest.mark.asyncio
async def test_textual_dispatch_suppresses_duplicate_tool_done(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_framework.textual_cli as textual_cli_module

    result = AgentRunResult(
        success=True,
        final_answer="done",
        usage=TokenUsage(total_tokens=5),
        iterations_used=1,
        iteration_history=[],
    )

    async def fake_stream(*args, **kwargs):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            data={"tool_name": "spawn_agent", "tool_call_id": "tc_1", "arguments": {}},
        )
        yield StreamEvent(
            type=StreamEventType.SUBAGENT_START,
            data={"tool_call_id": "tc_1", "index": 1, "total": 1, "task_input": "demo"},
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DONE,
            data={"tool_name": "spawn_agent", "tool_call_id": "tc_1", "success": True, "output": "ok"},
        )
        yield StreamEvent(
            type=StreamEventType.SUBAGENT_DONE,
            data={
                "tool_call_id": "tc_1",
                "tool_name": "spawn_agent",
                "index": 1,
                "total": 1,
                "success": True,
                "output": "done",
            },
        )
        yield StreamEvent(type=StreamEventType.DONE, data={"result": result})

    monkeypatch.setattr(textual_cli_module, "execute_user_input_stream", fake_stream)

    app = object.__new__(AegisAgentApp)
    app._fw = SimpleNamespace()
    app._mock = None
    app._state = ReplState()
    app._cancel_event = None
    app._header = SimpleNamespace(turn_count=0, total_tokens=0)
    app._set_busy = lambda busy: None

    captured: list[str] = []
    app._append_chat = lambda text: captured.append(text)
    app._append_chat_raw = lambda text: captured.append(text)

    await app._dispatch("task")

    text = "".join(captured)
    assert "[ok]" not in text
    assert "[subagent 1/1]" in text
