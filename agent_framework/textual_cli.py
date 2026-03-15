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
    execute_user_input_stream,
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
_MAX_CHAT_LINES = 1500
_MAX_PALETTE_RESULTS = 40

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── Role markers for chat output ──
_USER_PREFIX = "You > "
_AGENT_PREFIX = "\nAgent > "
_AGENT_RESUME = "\n  > "
_TOOL_PREFIX = "[tool] "
_TOOL_OK = "[ok]"
_TOOL_FAIL = "[FAIL]"
_ITER_PREFIX = "--- iter "
_META_PREFIX = "  --- "
_ERR_PREFIX = "  [error] "


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
        self._search_timer: Any = None

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
            if self._search_timer is not None:
                self._search_timer.stop()
            self._search_timer = self.set_timer(
                0.08, lambda: self._refresh(event.value)
            )

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
        ))[:_MAX_PALETTE_RESULTS]
    scored = [(score_palette_entry(query, e, recent), e) for e in entries]
    return [
        e
        for s, e in sorted(scored, key=lambda x: (-x[0], x[1].command))
        if s > 0
    ][:_MAX_PALETTE_RESULTS]


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
        self._palette_entries = build_palette_entries(self._fw)
        self._busy = False
        self._suppress_slash = False
        self._cancel_event: asyncio.Event | None = None
        self._status_timer: Any = None
        self._header: AegisHeader | None = None
        self._chat: TextArea | None = None
        self._prompt: Input | None = None
        self._busy_line: Static | None = None
        self._follow_output = True
        self._chat_end_location: tuple[int, int] = (0, 0)
        self._chat_has_content = False
        self._chat_line_count = 0
        self._pending_updates: list[tuple[str, bool]] = []
        self._flush_pending = False

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
        import uuid
        from pathlib import Path
        self._project_id = Path.cwd().name
        self._header = self.query_one("#hdr", AegisHeader)
        self._chat = self.query_one("#chat", TextArea)
        self._prompt = self.query_one("#prompt", Input)
        self._busy_line = self.query_one("#busy-line", Static)
        self._prompt.focus()
        # 从 DB 恢复最近一次会话历史
        if self._fw._memory_store:
            loaded = self._state.load_from_db(self._fw._memory_store, self._project_id)
            if loaded:
                self._append_chat(f"[已恢复 {loaded} 条历史消息 (conv: {self._state.conversation_id[:8]}...)]")
                summary = self._state.render_history_summary()
                if summary:
                    self._append_chat(_strip_ansi(summary))
        else:
            self._state.conversation_id = str(uuid.uuid4())
        # 异步连接 MCP/A2A
        self.call_later(self._setup_protocols)

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
        self._follow_output = True
        self._append_chat_block(["", _USER_PREFIX + text])
        self._set_busy(True)
        self.run_worker(self._dispatch(text), exclusive=True, group="agent")

    async def _dispatch(self, text: str) -> None:
        from agent_framework.models.stream import StreamEventType

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

            # Streaming output — tokens appear incrementally
            self._append_chat(_AGENT_PREFIX)
            in_tool_block = False
            subagent_tool_call_ids: set[str] = set()

            async for event in execute_user_input_stream(
                self._fw, self._mock, self._state, text,
                cancel_event=cancel_event,
            ):
                if event.type == StreamEventType.TOKEN:
                    token_text = event.data.get("text", "")
                    if token_text:
                        if in_tool_block:
                            # Resume agent text after tool block
                            self._append_chat(_AGENT_RESUME)
                            in_tool_block = False
                        self._append_chat_raw(token_text)

                elif event.type == StreamEventType.TOOL_CALL_START:
                    in_tool_block = True
                    tool_name = event.data.get("tool_name", "?")
                    self._append_chat(f"\n  {_TOOL_PREFIX}{tool_name}")

                elif event.type == StreamEventType.TOOL_CALL_DONE:
                    tool_call_id = str(event.data.get("tool_call_id", ""))
                    if tool_call_id in subagent_tool_call_ids and in_tool_block:
                        pass  # Suppress duplicate — SUBAGENT_DONE handles display
                    else:
                        success = event.data.get("success", False)
                        marker = _TOOL_OK if success else _TOOL_FAIL
                        self._append_chat_raw(f" {marker}")

                elif event.type == StreamEventType.ITERATION_START:
                    iteration_index = event.data.get("iteration_index", 0)
                    if iteration_index > 0:
                        self._append_chat(f"\n{_ITER_PREFIX}#{iteration_index + 1}")

                elif event.type == StreamEventType.DONE:
                    elapsed = time.monotonic() - t0
                    result = event.data.get("result")
                    tokens = result.usage.total_tokens if result else 0
                    self._append_chat(
                        f"\n{_META_PREFIX}{elapsed:.1f}s"
                        + (f" | {tokens:,} tokens" if tokens else "")
                    )
                    hdr = self._header
                    assert hdr is not None
                    hdr.turn_count = self._state.turn_count
                    hdr.total_tokens = self._state.total_tokens_estimate

                elif event.type == StreamEventType.SUBAGENT_START:
                    tool_call_id = str(event.data.get("tool_call_id", ""))
                    if tool_call_id:
                        subagent_tool_call_ids.add(tool_call_id)
                    idx = event.data.get("index", 0)
                    total = event.data.get("total", 0)
                    task_input = event.data.get("task_input", "")[:50]
                    self._append_chat(f"\n  [subagent {idx}/{total}] 启动: {task_input}")

                elif event.type == StreamEventType.SUBAGENT_DONE:
                    idx = event.data.get("index", 0)
                    total = event.data.get("total", 0)
                    success = event.data.get("success", False)
                    output = event.data.get("output", "")[:60]
                    status = "✓" if success else "✗"
                    self._append_chat(f"\n  [subagent {idx}/{total}] {status} {output}")

                elif event.type == StreamEventType.PROGRESSIVE_RESPONSE:
                    text_resp = event.data.get("text", "")
                    idx = event.data.get("index", 0)
                    total = event.data.get("total", 0)
                    self._append_chat(f"\n  Agent [{idx}/{total}]: {text_resp}")

                elif event.type == StreamEventType.ERROR:
                    error_msg = event.data.get("error", "unknown error")
                    self._append_chat(f"\n{_ERR_PREFIX}{error_msg}")

        except Exception as exc:
            self._append_chat(f"\n{_ERR_PREFIX}{exc}")
        finally:
            self._cancel_event = None
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        assert self._busy_line is not None
        if busy:
            self._busy_line.update("[bold yellow]  Working...[/]")
        elif not busy:
            self._busy_line.update("")
        assert self._header is not None
        self._header.is_busy = busy

    # ── Actions ────────────────────────────────────────

    def action_open_palette(self) -> None:
        self.push_screen(
            CommandPaletteScreen(
                self._palette_entries,
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
        self._show_status("Chat cleared")

    def action_new_session(self) -> None:
        self._state = ReplState()
        self._pending_updates.clear()
        self._clear_chat_view()
        hdr = self._header
        assert hdr is not None
        hdr.turn_count = 0
        hdr.total_tokens = 0
        # Reset conversation session so next run sends full context
        self._fw.end_conversation()
        self._fw.begin_conversation()
        self._show_status("New session started")
        assert self._prompt is not None
        self._prompt.focus()

    def action_focus_chat(self) -> None:
        assert self._chat is not None and self._header is not None
        self._chat.focus()
        self._follow_output = False
        self._header.focus_mode = "chat"
        self._show_status("Chat focused: Ctrl+C copy, mouse or PageUp/PageDown scroll")

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
            self._append_chat(f"\n{_META_PREFIX}[Cancelled]")
            self._set_busy(False)
        assert self._prompt is not None and self._header is not None
        self._follow_output = True
        self._prompt.focus()
        self._header.focus_mode = "input"

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "agent":
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._flush_updates()
            self._set_busy(False)
            assert self._prompt is not None and self._header is not None
            self._prompt.focus()
            self._header.focus_mode = "input"

    async def _setup_protocols(self) -> None:
        from agent_framework.terminal_runtime import _setup_protocols
        await _setup_protocols(self._fw)

    async def on_unmount(self) -> None:
        self._flush_updates()
        if self._fw._memory_store:
            self._state.save_to_db(self._fw._memory_store, self._project_id)
        await self._fw.shutdown()

    def _append_chat(self, text: str) -> None:
        self._push_update(text, is_raw=False)

    def _append_chat_raw(self, text: str) -> None:
        self._push_update(text, is_raw=True)

    def _push_update(self, text: str, is_raw: bool) -> None:
        if not text:
            return
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._pending_updates.append((normalized, is_raw))
        if not self._flush_pending:
            self._flush_pending = True
            self.set_timer(0.04, self._flush_updates)

    def _flush_updates(self) -> None:
        """Process buffered chat updates in a single batch to avoid UI lag."""
        self._flush_pending = False
        if not self._pending_updates or self._chat is None:
            return

        updates = self._pending_updates[:]
        self._pending_updates.clear()

        full_insertion = ""
        had_initial_content = self._chat_has_content
        for text, is_raw in updates:
            if not is_raw and (had_initial_content or full_insertion):
                full_insertion += "\n" + text
            else:
                full_insertion += text

        if not full_insertion:
            return

        with self.app.batch_update():
            self._chat.insert(
                full_insertion,
                location=self._chat_end_location,
                maintain_selection_offset=False,
            )
            self._chat_end_location = self._advance_location(
                self._chat_end_location, full_insertion
            )
            
            added_lines = full_insertion.count("\n")
            if not self._chat_has_content and full_insertion:
                added_lines += 1
            
            self._chat_has_content = True
            self._chat_line_count += added_lines
            
            self._trim_chat_if_needed()
            self._schedule_scroll()

    def _schedule_scroll(self) -> None:
        """Throttle scroll_end to at most once per 50ms to avoid UI lag."""
        if not self._follow_output:
            return
        now = time.monotonic()
        last = getattr(self, "_last_scroll_time", 0.0)
        if now - last < 0.05:
            if not getattr(self, "_scroll_pending", False):
                self._scroll_pending = True
                self.set_timer(0.05, self._do_deferred_scroll)
            return
        self._last_scroll_time = now
        assert self._chat is not None
        self._chat.scroll_end(animate=False)

    def _do_deferred_scroll(self) -> None:
        self._scroll_pending = False
        if self._follow_output and self._chat is not None:
            self._last_scroll_time = time.monotonic()
            self._chat.scroll_end(animate=False)

    def _append_chat_block(self, lines: list[str]) -> None:
        normalized_lines = [line.replace("\r\n", "\n").replace("\r", "\n") for line in lines]
        self._append_chat("\n".join(normalized_lines))

    def _clear_chat_view(self) -> None:
        assert self._chat is not None
        self._chat.load_text("")
        self._follow_output = True
        self._chat_end_location = (0, 0)
        self._chat_has_content = False
        self._chat_line_count = 0

    def _trim_chat_if_needed(self) -> None:
        if self._chat_line_count <= _MAX_CHAT_LINES:
            return
        assert self._chat is not None
        # Use batch_update to prevent multiple repaints during trim
        with self.app.batch_update():
            lines = self._chat.text.splitlines()
            trimmed_lines = lines[-_MAX_CHAT_LINES:]
            trimmed_text = "\n".join(trimmed_lines)
            self._chat.load_text(trimmed_text)
            self._chat_line_count = len(trimmed_lines)
            self._chat_has_content = bool(trimmed_text)
            self._chat_end_location = self._advance_location((0, 0), trimmed_text)

    def _show_status(self, message: str, timeout: float = 2.0) -> None:
        if self._busy:
            return
        assert self._busy_line is not None
        self._busy_line.update(f"[dim]  {message}[/]")
        if self._status_timer is not None:
            self._status_timer.stop()
        self._status_timer = self.set_timer(timeout, self._clear_status)

    def _clear_status(self) -> None:
        if self._busy:
            return
        assert self._busy_line is not None
        self._busy_line.update("")

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
