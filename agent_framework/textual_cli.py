"""AegisAgent — Textual TUI workspace.

Layout (no sidebar — clean copy-paste):
  Header: AegisAgent | model | tools | skills | config | turns | tokens
  ┌─ Chat (full width) ────────────────────────────────────────┐
  │  Logo → Welcome → conversation (all selectable/copyable)   │
  └────────────────────────────────────────────────────────────┘
  Working... (status line)
  ┌─ Input ──────────────────────────────────────────── [Send] ┐
  └────────────────────────────────────────────────────────────┘
  Footer: F10 Quit | Ctrl+P Cmds | Ctrl+L Clear | Ctrl+N New
"""

# NOTE: This module requires the following packages to be installed:
# - rich
# - textual

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)
from textual.worker import Worker, WorkerState

from agent_framework.terminal_runtime import (
    AEGIS_LOGO_LINES,
    CommandPaletteEntry,
    ReplState,
    build_palette_entries,
    execute_slash_command,
    execute_user_input,
    score_palette_entry,
)

# ── Colors ─────────────────────────────────────────────────
_GOLD = "#d9c07c"
_TEAL = "#5ccfe6"
_BG = "#0c1219"
_BG_PANEL = "#111b24"
_BG_SURFACE = "#192531"
_BG_INPUT = "#0e1820"
_BORDER = "#2e4455"
_DIM = "#6a7a8a"
_FG = "#ddd8d0"
_MAX_CHAT_LINES = 1200

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ══════════════════════════════════════════════════════════
#  Custom header with live status
# ══════════════════════════════════════════════════════════

class AegisHeader(Static):
    """Single-line header: brand + model + tools + turns + tokens."""

    model_name: reactive[str] = reactive("?")
    tool_count: reactive[int] = reactive(0)
    skill_count: reactive[int] = reactive(0)
    config_path: reactive[str] = reactive("")
    turn_count: reactive[int] = reactive(0)
    total_tokens: reactive[int] = reactive(0)
    is_busy: reactive[bool] = reactive(False)
    focus_mode: reactive[str] = reactive("input")

    DEFAULT_CSS = f"""
    AegisHeader {{
        dock: top;
        height: 1;
        background: {_BG_SURFACE};
        color: {_FG};
        padding: 0 1;
    }}
    """

    def render(self) -> str:
        status = "[yellow]working[/]" if self.is_busy else "[green]ready[/]"
        focus = (
            f"[bold cyan]input[/]"
            if self.focus_mode == "input"
            else f"[bold green]chat[/]"
        )
        return (
            f"[bold {_GOLD}]AegisAgent[/]"
            f"  [dim]|[/]  {status}"
            f"  [dim]|[/]  [dim]focus:[/] {focus}"
            f"  [dim]|[/]  [dim]model:[/] {self.model_name}"
            f"  [dim]|[/]  [dim]tools:[/] {self.tool_count}"
            f"  [dim]|[/]  [dim]skills:[/] {self.skill_count}"
            f"  [dim]|[/]  [dim]config:[/] {self.config_path or '-'}"
            f"  [dim]|[/]  [dim]turns:[/] {self.turn_count}"
            f"  [dim]|[/]  [dim]tokens:[/] {self.total_tokens:,}"
        )


# ══════════════════════════════════════════════════════════
#  Command palette
# ══════════════════════════════════════════════════════════

class CommandPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]

    DEFAULT_CSS = f"""
    CommandPaletteScreen {{
        align: center middle;
        background: rgba(8, 12, 18, 0.85);
    }}
    #pal-box {{
        width: 72;
        max-height: 80%;
        border: solid {_GOLD};
        background: {_BG_SURFACE};
        padding: 1 2;
    }}
    #pal-title {{
        text-align: center;
        color: {_GOLD};
        text-style: bold;
        margin-bottom: 1;
    }}
    #pal-search {{
        margin-bottom: 1;
        border: solid {_TEAL};
        background: {_BG_INPUT};
    }}
    #pal-list {{
        height: 1fr;
        min-height: 6;
        border: solid {_BORDER};
        background: {_BG_PANEL};
    }}
    #pal-list > ListItem {{
        padding: 0 1;
    }}
    #pal-list > ListItem.--highlight {{
        background: {_BG_SURFACE};
    }}
    #pal-hint {{
        margin-top: 1;
        color: {_DIM};
        text-align: center;
    }}
    """

    def __init__(self, entries: list[CommandPaletteEntry], recent: list[str]) -> None:
        super().__init__()
        self._entries = entries
        self._recent = recent
        self._list_view: ListView | None = None
        self._search_input: Input | None = None
        self._last_commands: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        with Container(id="pal-box"):
            yield Static("AegisAgent Commands", id="pal-title")
            yield Input(placeholder="Type to filter...", id="pal-search")
            yield ListView(id="pal-list")
            yield Static("Enter=run | Esc=close | Up/Down=navigate", id="pal-hint")

    def on_mount(self) -> None:
        self._search_input = self.query_one("#pal-search", Input)
        self._list_view = self.query_one("#pal-list", ListView)
        self._search_input.focus()
        self._refresh("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pal-search":
            self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter pressed while search input has focus — select highlighted item."""
        if event.input.id == "pal-search":
            self._select_highlighted()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, _CmdItem):
            self.dismiss(event.item.command)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        assert self._list_view is not None
        self._list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        assert self._list_view is not None
        self._list_view.action_cursor_up()

    def _select_highlighted(self) -> None:
        """Dismiss with the currently highlighted command."""
        assert self._list_view is not None
        lv = self._list_view
        h = lv.highlighted_child
        if isinstance(h, _CmdItem):
            self.dismiss(h.command)

    def _refresh(self, query: str) -> None:
        assert self._list_view is not None
        lv = self._list_view
        filtered = _filter_entries(self._entries, query, self._recent)
        command_keys = tuple(entry.command for entry in filtered)
        if command_keys == self._last_commands:
            return
        self._last_commands = command_keys
        lv.clear()
        for e in filtered:
            lv.append(_CmdItem(e))
        if filtered:
            lv.index = 0


class _CmdItem(ListItem):
    def __init__(self, entry: CommandPaletteEntry) -> None:
        cmd = entry.command
        desc = entry.description[:50]
        cat = entry.category
        super().__init__(Label(f"[bold cyan]{cmd}[/]  {desc}  [dim italic]({cat})[/]"))
        self.command = cmd


def _filter_entries(
    entries: list[CommandPaletteEntry], query: str, recent: list[str],
) -> list[CommandPaletteEntry]:
    if not query.strip():
        return sorted(entries, key=lambda e: (
            recent.index(e.command) if e.command in recent else 999, e.command,
        ))
    scored = [(score_palette_entry(query, e, recent), e) for e in entries]
    return [e for s, e in sorted(scored, key=lambda x: (-x[0], x[1].command)) if s > 0]


# ══════════════════════════════════════════════════════════
#  Main application
# ══════════════════════════════════════════════════════════

class AegisAgentApp(App[None]):
    TITLE = "AegisAgent"

    BINDINGS = [
        Binding("f10", "quit", show=False, priority=True),
        Binding("ctrl+p", "open_palette", priority=True, show=False),
        Binding("ctrl+l", "clear_chat", priority=True, show=False),
        Binding("ctrl+n", "new_session", priority=True, show=False),
        Binding("f6", "focus_chat", priority=True, show=False),
        Binding("escape", "focus_prompt", show=False, priority=True),
        Binding("pageup", "scroll_chat_up_page", show=False, priority=True),
        Binding("pagedown", "scroll_chat_down_page", show=False, priority=True),
        Binding("ctrl+up", "scroll_chat_up", show=False, priority=True),
        Binding("ctrl+down", "scroll_chat_down", show=False, priority=True),
    ]

    DEFAULT_CSS = f"""
    Screen {{
        background: {_BG};
        color: {_FG};
    }}

    #chat {{
        height: 1fr;
        margin: 0 1;
        border: solid {_BORDER};
        background: {_BG_PANEL};
        padding: 0 1;
    }}

    #busy-line {{
        height: 1;
        margin: 0 1;
        color: {_DIM};
    }}

    #input-bar {{
        dock: bottom;
        height: 3;
        margin: 0 1 0 1;
    }}
    #prompt {{
        width: 1fr;
        border: solid {_BORDER};
        background: {_BG_INPUT};
    }}
    #prompt:focus {{
        border: solid {_TEAL};
    }}
    #btn-send {{
        width: 8;
        margin-left: 1;
        background: {_BG_SURFACE};
        color: {_GOLD};
        border: solid {_BORDER};
    }}
    """

    def __init__(self, framework: Any, mock_model: Any, config_path: str | None) -> None:
        super().__init__()
        self._fw = framework
        self._mock = mock_model
        self._config_path = config_path
        self._state = ReplState()
        self._busy = False
        self._suppress_slash = False
        self._cancel_event: asyncio.Event | None = None
        self._header: AegisHeader | None = None
        self._chat: TextArea | None = None
        self._prompt: Input | None = None
        self._busy_line: Static | None = None
        self._chat_end_location: tuple[int, int] = (0, 0)
        self._chat_has_content = False
        self._chat_line_count = 0

    def compose(self) -> ComposeResult:
        yield AegisHeader(id="hdr")
        yield TextArea(
            "",
            id="chat",
            read_only=True,
            show_line_numbers=False,
            show_cursor=False,
            soft_wrap=True,
            compact=True,
            highlight_cursor_line=False,
            max_checkpoints=0,
        )
        yield Static("", id="busy-line")
        with Horizontal(id="input-bar"):
            yield Input(placeholder="Ask anything...  /=cmds  ^P=palette  ^L=clear  ^N=new  F6=chat  Esc=input  F10=quit", id="prompt")
            yield Button("Send", id="btn-send", variant="default")

    # ── Mount ──────────────────────────────────────────

    def on_mount(self) -> None:
        self._header = self.query_one("#hdr", AegisHeader)
        self._chat = self.query_one("#chat", TextArea)
        self._prompt = self.query_one("#prompt", Input)
        self._busy_line = self.query_one("#busy-line", Static)
        self._prompt.focus()

        hdr = self._header
        hdr.model_name = "Mock" if self._mock else self._fw.config.model.default_model_name
        hdr.tool_count = len(self._fw._registry.list_tools()) if self._fw._registry else 0
        hdr.skill_count = len(self._fw.list_skills())
        hdr.config_path = self._config_path or "-"
        hdr.focus_mode = "input"

        self._append_chat_block(
            [
                *AEGIS_LOGO_LINES,
                "Welcome! Type a question or /help for commands.",
                "",
            ]
        )

    # ── Input ──────────────────────────────────────────

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        if self._suppress_slash:
            self._suppress_slash = False
            return
        if event.value == "/":
            event.input.value = ""
            self.action_open_palette()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt":
            await self._submit_input()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-send":
            await self._submit_input()

    async def _submit_input(self) -> None:
        prompt = self._prompt
        assert prompt is not None
        text = prompt.value.strip()
        prompt.value = ""
        if not text or self._busy:
            return
        self._append_chat_block(["", f"You > {text}"])
        self._set_busy(True)
        self.run_worker(self._dispatch(text), exclusive=True, group="agent")

    async def _dispatch(self, text: str) -> None:
        t0 = time.monotonic()
        cancel_event = asyncio.Event()
        self._cancel_event = cancel_event
        try:
            if text.startswith("/"):
                result = await execute_slash_command(
                    self._fw, self._mock, self._state, text, ui_mode="textual",
                )
                if result.clear_output:
                    self._clear_chat_view()
                if result.output:
                    self._append_chat(_strip_ansi(result.output))
                if result.should_exit:
                    self.exit()
                return

            output = await execute_user_input(
                self._fw, self._mock, self._state, text,
                cancel_event=cancel_event,
                include_trace=False,
            )
            elapsed = time.monotonic() - t0
            self._append_chat(_strip_ansi(output))
            self._append_chat(f"({elapsed:.1f}s)")

            hdr = self._header
            assert hdr is not None
            hdr.turn_count = self._state.turn_count
            hdr.total_tokens = sum(len(m.content or "") // 4 for m in self._state.history)
        except Exception as exc:
            self._append_chat(f"Error: {exc}")
        finally:
            self._cancel_event = None
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        assert self._busy_line is not None
        self._busy_line.update(
            "[bold yellow]  Working...[/]" if busy else ""
        )
        assert self._header is not None
        self._header.is_busy = busy

    # ── Actions ────────────────────────────────────────

    def action_open_palette(self) -> None:
        self.push_screen(
            CommandPaletteScreen(
                build_palette_entries(self._fw),
                list(self._state.recent_commands),
            ),
            callback=self._palette_closed,
        )

    def _palette_closed(self, command: str | None) -> None:
        prompt = self._prompt
        assert prompt is not None
        if command:
            self._suppress_slash = True
            prompt.value = ""
            prompt.focus()
            self._append_chat_block(["", f"> {command}"])
            self._set_busy(True)
            self.run_worker(self._dispatch(command), exclusive=True, group="agent")
        else:
            prompt.focus()

    def action_clear_chat(self) -> None:
        self._clear_chat_view()
        self.notify("Chat cleared", timeout=2)

    def action_new_session(self) -> None:
        self._state = ReplState()
        self._clear_chat_view()
        hdr = self._header
        assert hdr is not None
        hdr.turn_count = 0
        hdr.total_tokens = 0
        # Reset conversation session so next run sends full context
        self._fw.end_conversation()
        self._fw.begin_conversation()
        self.notify("New session started", timeout=2)
        assert self._prompt is not None
        self._prompt.focus()

    def action_focus_chat(self) -> None:
        assert self._chat is not None and self._header is not None
        self._chat.focus()
        self._header.focus_mode = "chat"
        self.notify("Chat focused: select/copy with Ctrl+C, scroll with mouse or PageUp/PageDown", timeout=2)

    def action_scroll_chat_up_page(self) -> None:
        assert self._chat is not None
        self._chat.scroll_page_up(animate=False)

    def action_scroll_chat_down_page(self) -> None:
        assert self._chat is not None
        self._chat.scroll_page_down(animate=False)

    def action_scroll_chat_up(self) -> None:
        assert self._chat is not None
        self._chat.scroll_up(animate=False)

    def action_scroll_chat_down(self) -> None:
        assert self._chat is not None
        self._chat.scroll_down(animate=False)

    def action_focus_prompt(self) -> None:
        if self._busy and self._cancel_event:
            self._cancel_event.set()
            self.workers.cancel_group(self, "agent")
            self._append_chat("[Cancelled by user]")
            self._set_busy(False)
        assert self._prompt is not None and self._header is not None
        self._prompt.focus()
        self._header.focus_mode = "input"

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "agent":
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._set_busy(False)
            assert self._prompt is not None and self._header is not None
            self._prompt.focus()
            self._header.focus_mode = "input"

    async def on_unmount(self) -> None:
        await self._fw.shutdown()

    def _append_chat(self, text: str) -> None:
        assert self._chat is not None
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        had_content = self._chat_has_content
        insertion = normalized if not had_content else f"\n{normalized}"
        self._chat.insert(
            insertion,
            location=self._chat_end_location,
            maintain_selection_offset=False,
        )
        self._chat_end_location = self._advance_location(self._chat_end_location, insertion)
        self._chat_has_content = True
        added_lines = max(1, len(normalized.splitlines())) if normalized else 1
        self._chat_line_count = added_lines if not had_content else self._chat_line_count + added_lines
        self._trim_chat_if_needed()
        self._chat.scroll_end(animate=False)

    def _append_chat_block(self, lines: list[str]) -> None:
        normalized_lines = [line.replace("\r\n", "\n").replace("\r", "\n") for line in lines]
        self._append_chat("\n".join(normalized_lines))

    def _clear_chat_view(self) -> None:
        assert self._chat is not None
        self._chat.load_text("")
        self._chat_end_location = (0, 0)
        self._chat_has_content = False
        self._chat_line_count = 0

    def _trim_chat_if_needed(self) -> None:
        if self._chat_line_count <= _MAX_CHAT_LINES:
            return
        assert self._chat is not None
        lines = self._chat.text.splitlines()
        trimmed_lines = lines[-_MAX_CHAT_LINES:]
        trimmed_text = "\n".join(trimmed_lines)
        self._chat.load_text(trimmed_text)
        self._chat_line_count = len(trimmed_lines)
        self._chat_has_content = bool(trimmed_text)
        self._chat_end_location = self._advance_location((0, 0), trimmed_text)

    @staticmethod
    def _advance_location(
        location: tuple[int, int],
        inserted_text: str,
    ) -> tuple[int, int]:
        row, column = location
        parts = inserted_text.split("\n")
        if len(parts) == 1:
            return row, column + len(parts[0])
        return row + len(parts) - 1, len(parts[-1])


def run_textual_cli(framework: Any, mock_model: Any, config_path: str | None) -> None:
    AegisAgentApp(framework, mock_model, config_path).run(mouse=True)
