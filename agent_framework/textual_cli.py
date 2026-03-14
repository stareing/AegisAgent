"""AegisAgent — Textual TUI workspace.

Layout (no sidebar — clean copy-paste):
  Header: AegisAgent | model | tools | turns | tokens | clock
  ┌─ Chat (full width) ────────────────────────────────────────┐
  │  Logo → Welcome → conversation (all selectable/copyable)   │
  └────────────────────────────────────────────────────────────┘
  Working... (status line)
  ┌─ Input ──────────────────────────────────────────── [Send] ┐
  └────────────────────────────────────────────────────────────┘
  Footer: Ctrl+C Quit | Ctrl+P Cmds | Ctrl+L Clear | Ctrl+N New
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text
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
    RichLog,
    Static,
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

_B = "\033[1m"
_D = "\033[2m"
_R = "\033[0m"
_AC = "\033[36m"
_AG = "\033[32m"
_AY = "\033[33m"


# ══════════════════════════════════════════════════════════
#  Custom header with live status
# ══════════════════════════════════════════════════════════

class AegisHeader(Static):
    """Single-line header: brand + model + tools + turns + tokens."""

    model_name: reactive[str] = reactive("?")
    tool_count: reactive[int] = reactive(0)
    turn_count: reactive[int] = reactive(0)
    total_tokens: reactive[int] = reactive(0)
    is_busy: reactive[bool] = reactive(False)

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
        return (
            f"[bold {_GOLD}]AegisAgent[/]"
            f"  [dim]|[/]  {status}"
            f"  [dim]|[/]  [dim]model:[/] {self.model_name}"
            f"  [dim]|[/]  [dim]tools:[/] {self.tool_count}"
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

    def compose(self) -> ComposeResult:
        with Container(id="pal-box"):
            yield Static("AegisAgent Commands", id="pal-title")
            yield Input(placeholder="Type to filter...", id="pal-search")
            yield ListView(id="pal-list")
            yield Static("Enter=run | Esc=close | Up/Down=navigate", id="pal-hint")

    def on_mount(self) -> None:
        self.query_one("#pal-search", Input).focus()
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
        self.query_one("#pal-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#pal-list", ListView).action_cursor_up()

    def _select_highlighted(self) -> None:
        """Dismiss with the currently highlighted command."""
        lv = self.query_one("#pal-list", ListView)
        h = lv.highlighted_child
        if isinstance(h, _CmdItem):
            self.dismiss(h.command)

    def _refresh(self, query: str) -> None:
        lv = self.query_one("#pal-list", ListView)
        filtered = _filter_entries(self._entries, query, self._recent)
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
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("ctrl+q", "quit", show=False, priority=True),
        Binding("ctrl+p", "open_palette", priority=True, show=False),
        Binding("ctrl+l", "clear_chat", priority=True, show=False),
        Binding("ctrl+n", "new_session", priority=True, show=False),
        Binding("escape", "focus_prompt", show=False),
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

    def compose(self) -> ComposeResult:
        yield AegisHeader(id="hdr")
        yield RichLog(id="chat", wrap=True, markup=False, highlight=False, auto_scroll=True)
        yield Static("", id="busy-line")
        with Horizontal(id="input-bar"):
            yield Input(placeholder="Ask anything...  /=cmds  ^P=palette  ^L=clear  ^N=new  Esc=stop  ^C=quit", id="prompt")
            yield Button("Send", id="btn-send", variant="default")

    # ── Mount ──────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

        hdr = self.query_one("#hdr", AegisHeader)
        hdr.model_name = "Mock" if self._mock else self._fw.config.model.default_model_name
        hdr.tool_count = len(self._fw._registry.list_tools()) if self._fw._registry else 0

        chat = self.query_one("#chat", RichLog)
        mode = hdr.model_name
        tools = hdr.tool_count
        skills = len(self._fw.list_skills())
        cfg = f"  |  config: {self._config_path}" if self._config_path else ""

        for line in AEGIS_LOGO_LINES:
            chat.write(Text(line, style="bold cyan"))
        chat.write(Text(f"model: {mode}  |  tools: {tools}  |  skills: {skills}{cfg}", style="dim"))
        chat.write(Text(""))
        chat.write(Text.from_ansi(f"{_B}{_AY}Welcome!{_R} Type a question or {_B}/help{_R} for commands."))
        chat.write(Text(""))

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
        prompt = self.query_one("#prompt", Input)
        text = prompt.value.strip()
        prompt.value = ""
        if not text or self._busy:
            return
        chat = self.query_one("#chat", RichLog)
        chat.write(Text.from_ansi(f"\n{_B}{_AG}You >{_R} {text}"))
        self._set_busy(True)
        self.run_worker(self._dispatch(text), exclusive=True, group="agent")

    async def _dispatch(self, text: str) -> None:
        chat = self.query_one("#chat", RichLog)
        t0 = time.monotonic()
        cancel_event = asyncio.Event()
        self._cancel_event = cancel_event
        try:
            if text.startswith("/"):
                result = await execute_slash_command(
                    self._fw, self._mock, self._state, text, ui_mode="textual",
                )
                if result.clear_output:
                    chat.clear()
                if result.output:
                    chat.write(Text.from_ansi(result.output))
                if result.should_exit:
                    self.exit()
                return

            output = await execute_user_input(
                self._fw, self._mock, self._state, text,
                cancel_event=cancel_event,
            )
            elapsed = time.monotonic() - t0
            chat.write(Text.from_ansi(output))
            chat.write(Text.from_ansi(f"{_D}({elapsed:.1f}s){_R}"))

            hdr = self.query_one("#hdr", AegisHeader)
            hdr.turn_count = self._state.turn_count
            hdr.total_tokens += sum(len(m.content or "") // 4 for m in self._state.history)
        except Exception as exc:
            chat.write(Text(f"Error: {exc}", style="bold red"))
        finally:
            self._cancel_event = None
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#busy-line", Static).update(
            "[bold yellow]  Working...[/]" if busy else ""
        )
        self.query_one("#hdr", AegisHeader).is_busy = busy

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
        prompt = self.query_one("#prompt", Input)
        if command:
            self._suppress_slash = True
            prompt.value = ""
            prompt.focus()
            chat = self.query_one("#chat", RichLog)
            chat.write(Text.from_ansi(f"\n{_B}{_AG}> {command}{_R}"))
            self._set_busy(True)
            self.run_worker(self._dispatch(command), exclusive=True, group="agent")
        else:
            prompt.focus()

    def action_clear_chat(self) -> None:
        self.query_one("#chat", RichLog).clear()
        self.notify("Chat cleared", timeout=2)

    def action_new_session(self) -> None:
        self._state = ReplState()
        self.query_one("#chat", RichLog).clear()
        hdr = self.query_one("#hdr", AegisHeader)
        hdr.turn_count = 0
        hdr.total_tokens = 0
        self.notify("New session started", timeout=2)
        self.query_one("#prompt", Input).focus()

    def action_focus_prompt(self) -> None:
        if self._busy and self._cancel_event:
            self._cancel_event.set()
            self.workers.cancel_group(self, "agent")
            chat = self.query_one("#chat", RichLog)
            chat.write(Text("[Cancelled by user]", style="bold yellow"))
            self._set_busy(False)
        self.query_one("#prompt", Input).focus()

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "agent":
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._set_busy(False)
            self.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        await self._fw.shutdown()


def run_textual_cli(framework: Any, mock_model: Any, config_path: str | None) -> None:
    # mouse=False: terminal handles text selection natively (copy works everywhere)
    AegisAgentApp(framework, mock_model, config_path).run(mouse=False)
