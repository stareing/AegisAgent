from __future__ import annotations

import io
from collections import deque
from types import SimpleNamespace

from agent_framework.terminal_runtime import (CommandPaletteEntry, ReplState,
                                              _read_multiline_input,
                                              score_palette_entry)


def test_repl_state_records_recent_commands_in_mru_order() -> None:
    state = ReplState()
    state.record_command("/help")
    state.record_command("/tools")
    state.record_command("/help")

    assert state.recent_commands == ["/help", "/tools"]


def test_score_palette_entry_prefers_prefix_matches() -> None:
    prefix_entry = CommandPaletteEntry(
        command="/review",
        title="review",
        description="Review current changes",
        category="通用",
    )
    substring_entry = CommandPaletteEntry(
        command="/code-review",
        title="code-review",
        description="Review current changes",
        category="通用",
    )

    assert score_palette_entry("/re", prefix_entry) > score_palette_entry("/re", substring_entry)


def test_score_palette_entry_prefers_recent_commands_for_empty_query() -> None:
    entry = CommandPaletteEntry(
        command="/help",
        title="help",
        description="Show help",
        category="通用",
    )

    assert score_palette_entry("", entry, ["/help"]) > score_palette_entry("", entry, [])


def test_read_multiline_input_supports_xml_style_block(monkeypatch) -> None:
    inputs = deque(["<text>", "line 1", "line 2", "</text>"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": inputs.popleft())

    result = _read_multiline_input("> ")

    assert result == "<text>\nline 1\nline 2\n</text>"


def test_read_multiline_input_supports_fenced_code_block(monkeypatch) -> None:
    inputs = deque(["```markdown", "# title", "```"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": inputs.popleft())

    result = _read_multiline_input("> ")

    assert result == "```markdown\n# title\n```"


def test_read_multiline_input_drains_buffered_paste(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "first line")

    class _FakeStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    fake_stdin = _FakeStdin("second line\nthird line\n")
    monkeypatch.setattr("sys.stdin", fake_stdin)

    ready_sequence = deque([([fake_stdin], [], []), ([fake_stdin], [], []), ([], [], []), ([], [], [])])
    monkeypatch.setattr(
        "select.select",
        lambda _r, _w, _x, _timeout=0.0: ready_sequence.popleft() if ready_sequence else ([], [], []),
    )

    result = _read_multiline_input("> ")

    assert result == "first line\nsecond line\nthird line"
