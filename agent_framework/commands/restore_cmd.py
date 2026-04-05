"""Slash command: /restore — git checkpoint-based restoration.

Ported from Gemini CLI's /restore command. Manages checkpoint snapshots
that capture conversation history, git state, and pending tool calls,
allowing the user to roll back to a previous point in the session.

Usage:
    /restore           — list available checkpoints
    /restore <id>      — restore to the checkpoint whose ID starts with <id>
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator

from pydantic import BaseModel, Field

from agent_framework.commands.protocol import (
    CommandActionReturn,
    LoadHistoryAction,
    MessageAction,
    SlashCommand,
    ToolAction,
)

if TYPE_CHECKING:
    from agent_framework.commands.protocol import CommandContext

# ---------------------------------------------------------------------------
# Default checkpoint directory (relative to working directory)
# ---------------------------------------------------------------------------

DEFAULT_CHECKPOINT_DIR = "data/checkpoints/"


# ---------------------------------------------------------------------------
# CheckpointData model
# ---------------------------------------------------------------------------

class CheckpointData(BaseModel):
    """Serializable snapshot of agent state at a point in time."""

    checkpoint_id: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 datetime when the checkpoint was created",
    )
    git_commit_hash: str | None = None
    conversation_messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Serialized conversation messages at checkpoint time",
    )
    tool_call: dict[str, Any] | None = Field(
        default=None,
        description="Original tool call that was checkpointed for replay",
    )
    description: str = ""


# ---------------------------------------------------------------------------
# Core checkpoint functions
# ---------------------------------------------------------------------------

def list_checkpoints(checkpoint_dir: str) -> list[CheckpointData]:
    """Read all checkpoint JSON files from *checkpoint_dir*.

    Returns a list of CheckpointData sorted by creation time (newest first).
    Non-parseable files are silently skipped.
    """
    if not os.path.isdir(checkpoint_dir):
        return []

    checkpoints: list[CheckpointData] = []

    for filename in os.listdir(checkpoint_dir):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(checkpoint_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as fh:
                raw = json.load(fh)
            checkpoints.append(CheckpointData.model_validate(raw))
        except (json.JSONDecodeError, ValueError, OSError):
            # Skip corrupt or unparseable files
            continue

    # Newest first
    checkpoints.sort(key=lambda c: c.created_at, reverse=True)
    return checkpoints


def save_checkpoint(checkpoint_dir: str, data: CheckpointData) -> str:
    """Persist *data* as a JSON file in *checkpoint_dir*.

    Returns the absolute path of the written file.
    """
    os.makedirs(checkpoint_dir, exist_ok=True)
    filename = f"{data.checkpoint_id}.json"
    filepath = os.path.join(checkpoint_dir, filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(data.model_dump_json(indent=2))

    return os.path.abspath(filepath)


# ---------------------------------------------------------------------------
# Restore logic (async generator yielding actions)
# ---------------------------------------------------------------------------

async def perform_restore(
    checkpoint: CheckpointData,
) -> AsyncGenerator[CommandActionReturn, None]:
    """Restore agent state from *checkpoint*.

    Yields a sequence of CommandActionReturn actions:
    1. LoadHistoryAction — restore conversation history
    2. Git checkout      — if git_commit_hash is present
    3. MessageAction     — success / failure notification
    4. ToolAction        — re-execute the original tool call (if any)
    """
    # Step 1: restore conversation history
    if checkpoint.conversation_messages:
        yield LoadHistoryAction(messages=checkpoint.conversation_messages)  # type: ignore[arg-type]

    # Step 2: git restore (stash + checkout)
    if checkpoint.git_commit_hash:
        commit_hash = checkpoint.git_commit_hash
        try:
            # Stash uncommitted work so checkout is clean
            stash_proc = await asyncio.create_subprocess_exec(
                "git", "stash", "--include-untracked",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            await stash_proc.wait()

            # Checkout the checkpoint commit
            checkout_proc = await asyncio.create_subprocess_exec(
                "git", "checkout", commit_hash,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr_bytes = await checkout_proc.communicate()

            if checkout_proc.returncode != 0:
                stderr_text = stderr_bytes.decode(errors="replace").strip()
                if "unable to read tree" in stderr_text:
                    yield MessageAction(
                        message_type="error",
                        content=(
                            f"The commit hash '{commit_hash}' associated with "
                            f"this checkpoint could not be found in your Git "
                            f"repository. This can happen if the repository has "
                            f"been re-cloned, reset, or if old commits have been "
                            f"garbage collected. This checkpoint cannot be restored."
                        ),
                    )
                    return
                yield MessageAction(
                    message_type="error",
                    content=f"Git checkout failed: {stderr_text}",
                )
                return

            yield MessageAction(
                message_type="info",
                content="Restored project to the state before the tool call.",
            )
        except FileNotFoundError:
            yield MessageAction(
                message_type="error",
                content=(
                    "Git is not available on this system. "
                    "Cannot restore file-system state from checkpoint."
                ),
            )
            return
        except OSError as exc:
            yield MessageAction(
                message_type="error",
                content=f"Git restore failed: {exc}",
            )
            return
    else:
        # No git state to restore — just confirm conversation reload
        yield MessageAction(
            message_type="info",
            content=(
                f"Checkpoint '{checkpoint.checkpoint_id}' restored "
                f"(conversation history only, no git state)."
            ),
        )

    # Step 4: re-execute the original tool call
    if checkpoint.tool_call is not None:
        tool_name = checkpoint.tool_call.get("tool_name", "")
        tool_args = checkpoint.tool_call.get("tool_args", {})
        if tool_name:
            yield ToolAction(tool_name=tool_name, tool_args=tool_args)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def _handle_restore(
    context: CommandContext,
    args: str = "",
) -> CommandActionReturn:
    """Execute the /restore command.

    - No args: list all available checkpoints.
    - With args: treat the argument as a checkpoint_id prefix and restore it.
    """
    checkpoint_dir = _resolve_checkpoint_dir(context)
    args = args.strip()

    if not args:
        return _format_checkpoint_list(checkpoint_dir)

    # Find checkpoint matching the prefix
    checkpoints = list_checkpoints(checkpoint_dir)
    matches = [
        cp for cp in checkpoints
        if cp.checkpoint_id.startswith(args)
    ]

    if not matches:
        return MessageAction(
            message_type="error",
            content=f"No checkpoint found matching '{args}'.",
        )

    if len(matches) > 1:
        ids = ", ".join(m.checkpoint_id for m in matches)
        return MessageAction(
            message_type="error",
            content=(
                f"Ambiguous checkpoint prefix '{args}' matches "
                f"{len(matches)} checkpoints: {ids}. "
                f"Please provide a longer prefix."
            ),
        )

    checkpoint = matches[0]

    # Collect all actions from the async generator and return the last
    # meaningful one (the integration layer should ideally iterate the
    # generator, but we return a summary for simple dispatch).
    results: list[CommandActionReturn] = []
    async for action in perform_restore(checkpoint):
        results.append(action)

    if not results:
        return MessageAction(
            message_type="info",
            content=f"Checkpoint '{checkpoint.checkpoint_id}' is empty.",
        )

    # Return the final action (typically the success message or tool action)
    return results[-1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_checkpoint_dir(context: CommandContext) -> str:
    """Determine the checkpoint directory from context or default."""
    if hasattr(context, "config") and context.config is not None:
        config_dir = getattr(context.config, "checkpoint_dir", None)
        if config_dir:
            return str(config_dir)

    return DEFAULT_CHECKPOINT_DIR


def _format_checkpoint_list(checkpoint_dir: str) -> MessageAction:
    """Build a human-readable listing of available checkpoints."""
    checkpoints = list_checkpoints(checkpoint_dir)

    if not checkpoints:
        return MessageAction(
            message_type="info",
            content="No checkpoints found.",
        )

    lines = ["Available checkpoints:\n"]
    for cp in checkpoints:
        ts = cp.created_at
        git_info = f" [git: {cp.git_commit_hash[:8]}]" if cp.git_commit_hash else ""
        tool_info = ""
        if cp.tool_call:
            tool_name = cp.tool_call.get("tool_name", "unknown")
            tool_info = f" (tool: {tool_name})"
        desc = f" — {cp.description}" if cp.description else ""

        lines.append(f"  {cp.checkpoint_id}  {ts}{git_info}{tool_info}{desc}")

    return MessageAction(message_type="info", content="\n".join(lines))


def _complete_checkpoint_ids(partial: str) -> list[str]:
    """Tab-completion: return checkpoint IDs matching *partial*.

    Uses the default checkpoint directory since the completer protocol
    does not receive the full CommandContext.
    """
    checkpoints = list_checkpoints(DEFAULT_CHECKPOINT_DIR)
    return [
        cp.checkpoint_id
        for cp in checkpoints
        if cp.checkpoint_id.startswith(partial)
    ]


# ---------------------------------------------------------------------------
# Exported SlashCommand instance
# ---------------------------------------------------------------------------

restore_command = SlashCommand(
    name="restore",
    description=(
        "Restore to a previous checkpoint, resetting conversation "
        "and file history to the checkpointed state"
    ),
    handler=_handle_restore,
    category="project",
    completer=_complete_checkpoint_ids,
)
