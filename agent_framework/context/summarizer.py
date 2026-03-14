"""Shared summarization logic for both REPL-level and context-level compression.

Single source of truth for:
- History-to-text conversion
- Summary XML tag format
- LLM compression call
- Summary validation
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message

logger = get_logger(__name__)

# Canonical summary tag — used by REPL compact, context compressor, and
# context source provider recognition.
SUMMARY_TAG = "summary"


def messages_to_text(messages: list[Message]) -> str:
    """Convert a list of Messages to a text representation for compression.

    Single source: both REPL compact() and ContextCompressor._llm_summarize()
    use this function. No content truncation.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.role
        content = msg.content or ""

        if msg.tool_calls:
            args_parts = []
            for tc in msg.tool_calls:
                args_str = json.dumps(tc.arguments, ensure_ascii=False, default=str)
                args_parts.append(f"{tc.function_name}({args_str})")
            lines.append(f"[{role}] (tool_calls: {', '.join(args_parts)})")
        elif msg.tool_call_id:
            lines.append(f"[tool:{msg.name or '?'}] {content}")
        else:
            lines.append(f"[{role}] {content}")

    return "\n".join(lines)


def wrap_summary(summary_text: str, version: int = 1) -> str:
    """Wrap summary text in canonical XML tag."""
    return f"<{SUMMARY_TAG} version=\"{version}\">\n{summary_text}\n</{SUMMARY_TAG}>"


def is_summary_message(msg: Message) -> bool:
    """Check if a message is a compressed summary."""
    if not msg.content:
        return False
    return msg.content.strip().startswith(f"<{SUMMARY_TAG}")


async def call_llm_compress(
    history_text: str,
    model_adapter: Any,
    previous_summary: str | None = None,
    max_tokens: int = 2048,
) -> str | None:
    """Call LLM with compression prompt to produce structured summary.

    Args:
        history_text: Text representation of messages to compress.
        model_adapter: Model adapter for LLM call.
        previous_summary: Existing summary to merge with (for incremental).
        max_tokens: Max output tokens for the summary.

    Returns:
        Summary text string, or None on failure.
    """
    from agent_framework.agent.prompt_templates import CONTEXT_COMPRESSION_PROMPT

    input_parts: list[str] = []
    if previous_summary:
        input_parts.append(f"[已有摘要]\n{previous_summary}\n")
    input_parts.append(f"以下是需要压缩的历史对话：\n\n{history_text}")

    compress_messages = [
        Message(role="system", content=CONTEXT_COMPRESSION_PROMPT),
        Message(role="user", content="\n".join(input_parts)),
    ]

    try:
        response = await model_adapter.complete(
            messages=compress_messages,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        text = (response.content or "").strip()
        return text if text else None
    except Exception as e:
        logger.error("summarizer.llm_compress_failed", error=str(e))
        return None
