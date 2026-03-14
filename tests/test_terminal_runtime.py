from __future__ import annotations

from types import SimpleNamespace

from agent_framework.terminal_runtime import CommandPaletteEntry, ReplState, score_palette_entry


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

