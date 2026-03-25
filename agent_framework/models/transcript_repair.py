"""Session transcript repair — fixes malformed tool calls in history.

Validates and sanitizes tool call blocks to prevent errors during
session replay. Handles corrupted names, missing IDs, and orphaned
tool results.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Valid tool call name pattern (OC-compatible)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")

# Maximum reasonable argument size (prevent memory issues)
_MAX_ARGS_SIZE = 100_000


def is_valid_tool_name(name: str) -> bool:
    """Check if a tool name matches the allowed pattern."""
    return bool(_TOOL_NAME_RE.match(name))


def sanitize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any] | None:
    """Sanitize a single tool call block.

    Returns sanitized tool call, or None if unrecoverable.
    """
    if not isinstance(tool_call, dict):
        return None

    # Ensure required fields
    call_id = tool_call.get("id") or tool_call.get("tool_call_id")
    if not call_id or not isinstance(call_id, str):
        call_id = f"repair_{uuid.uuid4().hex[:8]}"

    name = tool_call.get("function_name") or tool_call.get("name", "")
    if not isinstance(name, str) or not name:
        logger.warning("transcript_repair.missing_tool_name", call_id=call_id)
        return None

    # Sanitize name
    if not is_valid_tool_name(name):
        # Try to fix common issues
        sanitized = re.sub(r"[^A-Za-z0-9_:.-]", "_", name)[:64]
        if not sanitized:
            return None
        logger.warning(
            "transcript_repair.name_sanitized",
            original=name[:64],
            sanitized=sanitized,
        )
        name = sanitized

    # Validate arguments
    args = tool_call.get("arguments") or tool_call.get("input", {})
    if isinstance(args, str):
        if len(args) > _MAX_ARGS_SIZE:
            args = args[:_MAX_ARGS_SIZE]
    elif isinstance(args, dict):
        pass  # OK
    else:
        args = {}

    return {
        "id": call_id,
        "function_name": name,
        "arguments": args,
    }


def repair_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair a list of session messages.

    Fixes:
    - Malformed tool call blocks
    - Orphaned tool results (no matching tool call)
    - Missing tool call IDs
    """
    # Collect valid tool call IDs
    valid_call_ids: set[str] = set()
    repaired: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")

        # Repair tool calls in assistant messages
        if role == "assistant" and "tool_calls" in msg:
            tool_calls = msg["tool_calls"]
            if isinstance(tool_calls, list):
                sanitized_calls = []
                for tc in tool_calls:
                    sanitized = sanitize_tool_call(tc)
                    if sanitized:
                        sanitized_calls.append(sanitized)
                        valid_call_ids.add(sanitized["id"])
                msg = {**msg, "tool_calls": sanitized_calls}

        # Remove orphaned tool results
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            if tool_call_id and tool_call_id not in valid_call_ids:
                logger.warning(
                    "transcript_repair.orphaned_tool_result",
                    tool_call_id=tool_call_id,
                )
                continue  # Skip orphaned result

        repaired.append(msg)

    repair_count = len(messages) - len(repaired)
    if repair_count > 0:
        logger.info("transcript_repair.completed", removed=repair_count)

    return repaired
