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

# Maximum compression ratio — summary output ≤ 15% of input tokens.
_MAX_COMPRESSION_RATIO = 0.15
# Absolute floor so very short histories still get a usable summary.
_MIN_SUMMARY_TOKENS = 256


def messages_to_text(messages: list[Message]) -> str:
    """Convert a list of Messages to a text representation for compression.

    Single source: both REPL compact() and ContextCompressor._llm_summarize()
    use this function. No content truncation.

    Uses .text_content to extract text from both plain and multimodal messages.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.role
        content = msg.text_content or ""

        if msg.tool_calls:
            args_parts = []
            for tc in msg.tool_calls:
                args_str = json.dumps(tc.arguments, ensure_ascii=False, default=str)
                args_parts.append(f"{tc.function_name}({args_str})")
            lines.append(f"[{role}] (tool_calls: {', '.join(args_parts)})")
        elif msg.tool_call_id:
            lines.append(f"[tool:{msg.name or '?'}] {content}")
        else:
            # Annotate multimodal messages so the summary preserves awareness
            if msg.has_multimodal:
                media_types = [p.type for p in (msg.content_parts or []) if p.type != "text"]
                suffix = f" [+{', '.join(media_types)}]"
                lines.append(f"[{role}] {content}{suffix}")
            else:
                lines.append(f"[{role}] {content}")

    return "\n".join(lines)


def wrap_summary(summary_text: str, version: int = 1) -> str:
    """Wrap summary text in canonical XML tag."""
    return f"<{SUMMARY_TAG} version=\"{version}\">\n{summary_text}\n</{SUMMARY_TAG}>"


def has_multimodal_content(msg: Message) -> bool:
    """Check if a message contains non-text content parts (images, audio, files)."""
    return msg.has_multimodal


def is_summary_message(msg: Message) -> bool:
    """Check if a message is a compressed summary."""
    text = msg.text_content
    if not text:
        return False
    return text.strip().startswith(f"<{SUMMARY_TAG}")


async def call_llm_compress(
    history_text: str,
    model_adapter: Any,
    previous_summary: str | None = None,
    max_tokens: int | None = None,
    messages: list[Message] | None = None,
) -> str | None:
    """Call LLM with compression prompt to produce structured summary.

    The output token budget is capped at 15% of input token count
    (``_MAX_COMPRESSION_RATIO``).  Callers may still pass an explicit
    *max_tokens* to further limit output.

    Args:
        history_text: Text representation of messages to compress.
        model_adapter: Model adapter for LLM call.
        previous_summary: Existing summary to merge with (for incremental).
        max_tokens: Optional hard cap on output tokens.
        messages: Original Message objects (unused, kept for API compat).

    Returns:
        Summary text string, or None on failure.
    """
    from agent_framework.agent.prompt_templates import \
        CONTEXT_COMPRESSION_PROMPT

    input_parts: list[str] = []
    if previous_summary:
        input_parts.append(f"[已有摘要]\n{previous_summary}\n")
    input_parts.append(f"以下是需要压缩的对话历史：\n\n{history_text}")
    user_content = "\n".join(input_parts)

    # Derive output budget: input_tokens × 15%, floor 256
    input_char_count = len(history_text) + len(previous_summary or "")
    estimated_input_tokens = max(input_char_count // 4, 1)
    budget = max(int(estimated_input_tokens * _MAX_COMPRESSION_RATIO), _MIN_SUMMARY_TOKENS)
    if max_tokens is not None:
        budget = min(budget, max_tokens)

    compress_messages = [
        Message(role="system", content=CONTEXT_COMPRESSION_PROMPT),
        Message(role="user", content=user_content),
    ]

    logger.info(
        "summarizer.compressing",
        input_tokens_est=estimated_input_tokens,
        output_budget=budget,
    )

    try:
        response = await model_adapter.complete(
            messages=compress_messages,
            temperature=0.0,
            max_tokens=budget,
        )
        text = (response.content or "").strip()
        return text if text else None
    except Exception as e:
        logger.error("summarizer.llm_compress_failed", error=str(e))
        return None
