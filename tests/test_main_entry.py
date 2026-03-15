from __future__ import annotations

import pytest

import agent_framework.main as main_module
from agent_framework.textual_cli import AegisAgentApp, _filter_entries
from agent_framework.terminal_runtime import CommandPaletteEntry, ReplState, format_result
from agent_framework.infra.config import FrameworkConfig
from agent_framework.models.agent import AgentRunResult, IterationResult
from agent_framework.models.message import Message, ModelResponse, TokenUsage


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


def test_textual_chat_location_advances_incrementally() -> None:
    assert AegisAgentApp._advance_location((0, 0), "hello") == (0, 5)
    assert AegisAgentApp._advance_location((0, 5), "\nworld") == (1, 5)
    assert AegisAgentApp._advance_location((1, 5), "\nline2\nline3") == (3, 5)
    assert AegisAgentApp._advance_location((0, 0), "a\nb\n") == (2, 0)


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
