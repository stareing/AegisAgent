"""Command registry: register, lookup, dispatch, and complete slash commands."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_framework.commands.protocol import (
    CommandActionReturn,
    CommandContext,
    MessageAction,
    SlashCommand,
)

logger = logging.getLogger(__name__)


class CommandRegistry:
    """Central registry for slash commands.

    Provides name/alias lookup, hierarchical sub-command dispatch,
    and tab-completion delegation.
    """

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._alias_index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, command: SlashCommand) -> None:
        """Register a slash command (overwrites if name already exists)."""
        if command.name in self._commands:
            logger.warning("command_overwrite", extra={"name": command.name})

        self._commands[command.name] = command

        for alias in command.aliases:
            self._alias_index[alias] = command.name

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> SlashCommand | None:
        """Resolve a command by name or alias. Returns ``None`` if not found."""
        if name in self._commands:
            return self._commands[name]

        canonical = self._alias_index.get(name)
        if canonical is not None:
            return self._commands.get(canonical)

        return None

    def list_all(self) -> list[SlashCommand]:
        """Return all registered commands (excluding hidden ones)."""
        return [cmd for cmd in self._commands.values() if not cmd.hidden]

    def list_by_category(self) -> dict[str, list[SlashCommand]]:
        """Group visible commands by category."""
        categories: dict[str, list[SlashCommand]] = {}
        for cmd in self._commands.values():
            if cmd.hidden:
                continue
            categories.setdefault(cmd.category, []).append(cmd)
        return categories

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        name: str,
        context: CommandContext,
        args: str = "",
    ) -> CommandActionReturn | None:
        """Dispatch a command by *name* (or alias) with *args*.

        Hierarchical sub-command resolution:
        1. Parse the first token of *args* as a potential sub-command name.
        2. If a matching sub-command exists, delegate to its handler with the
           remaining args.
        3. Otherwise, call the parent command's handler with the full *args*.

        Returns the action produced by the handler, or a ``MessageAction``
        error if the command is not found / has no handler.
        """
        command = self.get(name)
        if command is None:
            return MessageAction(
                message_type="error",
                content=f"Unknown command: {name}",
            )

        # --- sub-command resolution ---
        handler, effective_args = self._resolve_handler(command, args)

        if handler is None:
            return MessageAction(
                message_type="error",
                content=f"Command '/{name}' has no handler.",
            )

        context_with_args = context.model_copy(update={"args": effective_args})

        result: Any = handler(context_with_args, effective_args)

        # Support both sync and async handlers transparently.
        if asyncio.iscoroutine(result):
            result = await result

        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(self, name: str, partial: str) -> list[str]:
        """Return tab-completion candidates for *partial* within command *name*.

        Falls back to sub-command name completion when the command has no
        dedicated completer.
        """
        command = self.get(name)
        if command is None:
            return []

        if command.completer is not None:
            return command.completer(partial)

        # Default: complete against sub-command names.
        if command.subcommands:
            return [
                sub.name
                for sub in command.subcommands
                if sub.name.startswith(partial)
            ]

        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_handler(
        command: SlashCommand,
        args: str,
    ) -> tuple[Any, str]:
        """Walk sub-commands to find the deepest matching handler.

        Returns ``(handler_callable | None, remaining_args)``.
        """
        if not args or not command.subcommands:
            return command.handler, args

        parts = args.split(maxsplit=1)
        sub_name = parts[0]
        remaining = parts[1] if len(parts) > 1 else ""

        for sub in command.subcommands:
            if sub.name == sub_name or sub_name in sub.aliases:
                # Recurse into the sub-command.
                return CommandRegistry._resolve_handler(sub, remaining)

        # No matching sub-command; fall back to parent handler.
        return command.handler, args
