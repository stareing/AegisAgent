"""Typed command action return protocol and slash command model.

Inspired by Gemini CLI's typed action return pattern, adapted for
the agent framework's pydantic v2 architecture.

Action types:
- MessageAction: display info/error/warning to the user
- ToolAction: programmatically invoke a registered tool
- SubmitPromptAction: inject a prompt into the agent loop
- LoadHistoryAction: replace or prepend conversation history
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal, Union

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agent_framework.models.message import Message


# ---------------------------------------------------------------------------
# Action models (frozen, immutable value objects)
# ---------------------------------------------------------------------------

class MessageAction(BaseModel, frozen=True):
    """Display a message to the user without entering the agent loop."""

    type: Literal["message"] = "message"
    message_type: Literal["info", "error", "warning"] = "info"
    content: str


class ToolAction(BaseModel, frozen=True):
    """Programmatically trigger a tool invocation."""

    type: Literal["tool"] = "tool"
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)


class SubmitPromptAction(BaseModel, frozen=True):
    """Inject a prompt into the agent loop as if the user typed it."""

    type: Literal["submit_prompt"] = "submit_prompt"
    content: str


class LoadHistoryAction(BaseModel, frozen=True):
    """Replace or prepend conversation history with the given messages."""

    type: Literal["load_history"] = "load_history"
    messages: list[Message] = Field(default_factory=list)


CommandActionReturn = Union[
    MessageAction,
    ToolAction,
    SubmitPromptAction,
    LoadHistoryAction,
]
"""Union of all action types a slash command handler may return."""


# ---------------------------------------------------------------------------
# Command context (passed to every handler)
# ---------------------------------------------------------------------------

class CommandContext(BaseModel):
    """Runtime context supplied to every slash command handler.

    Uses ``Any`` for framework/config/state to avoid circular imports
    and to stay decoupled from concrete runtime types.
    """

    model_config = {"arbitrary_types_allowed": True}

    framework: Any = None
    config: Any = None
    state: Any = None
    args: str = ""


# ---------------------------------------------------------------------------
# Slash command model
# ---------------------------------------------------------------------------

# Handler and completer callable signatures (not pydantic fields):
#   handler:    async (CommandContext, str) -> CommandActionReturn | None
#   completer:  (str) -> list[str]

CommandHandler = Callable[..., Any]
CommandCompleter = Callable[[str], list[str]]


class SlashCommand(BaseModel):
    """Definition of a single slash command (e.g. ``/help``, ``/memory show``).

    Supports hierarchical sub-commands: ``/memory show`` resolves to the
    ``show`` entry inside ``memory.subcommands``.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    category: str = "general"
    hidden: bool = False
    subcommands: list[SlashCommand] = Field(default_factory=list)
    handler: CommandHandler | None = None
    completer: CommandCompleter | None = None
