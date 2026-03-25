"""Slash command protocol and registry for the agent framework.

Provides a typed action-return pattern inspired by Gemini CLI,
adapted for pydantic v2 and async-first Python.
"""

from agent_framework.commands.protocol import (
    CommandActionReturn,
    CommandContext,
    CommandHandler,
    LoadHistoryAction,
    MessageAction,
    SlashCommand,
    SubmitPromptAction,
    ToolAction,
)
from agent_framework.commands.registry import CommandRegistry
from agent_framework.commands.init_cmd import init_command
from agent_framework.commands.restore_cmd import restore_command
from agent_framework.commands.memory_cmd import memory_command
from agent_framework.commands.model_cmd import model_command
from agent_framework.commands.plugin_cmd import plugins_command

__all__ = [
    "CommandActionReturn",
    "CommandContext",
    "CommandHandler",
    "CommandRegistry",
    "LoadHistoryAction",
    "MessageAction",
    "SlashCommand",
    "SubmitPromptAction",
    "ToolAction",
    "init_command",
    "restore_command",
    "memory_command",
    "model_command",
    "plugins_command",
]
