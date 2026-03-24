"""Think tag parser — extracts reasoning blocks from LLM streaming output.

Handles multiple tag variants used by different providers:
<think>, <thinking>, <thought>, <antthinking> (case-insensitive).
Also strips leaked model control tokens from various providers.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple


class SegmentType(str, Enum):
    """Type of parsed stream segment."""

    TEXT = "text"
    THINKING = "thinking"
    THINKING_END = "thinking_end"


class ParsedSegment(NamedTuple):
    """A typed segment from stream parsing."""

    type: SegmentType
    content: str


class ThinkTagState:
    """Mutable state for stateful think tag parsing across stream chunks."""

    __slots__ = ("in_thinking", "_buffer")

    def __init__(self) -> None:
        self.in_thinking: bool = False
        self._buffer: str = ""


# Tag variants across providers (case-insensitive, whitespace-tolerant)
_THINK_OPEN_RE = re.compile(
    r"<\s*(?:think(?:ing)?|thought|antthinking)\s*>",
    re.IGNORECASE,
)
_THINK_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:think(?:ing)?|thought|antthinking)\s*>",
    re.IGNORECASE,
)

# Leaked model control tokens to strip
_MODEL_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_FULLWIDTH_SPECIAL_TOKEN_RE = re.compile(r"<\uff5c[^\uff5c]*\uff5c>")
_MINIMAX_INVOKE_RE = re.compile(r"</?invoke>|</minimax:tool_call>", re.IGNORECASE)
_DOWNGRADED_TOOL_RE = re.compile(r"\[Tool Call: [^\]]*\]|\[Historical context[^\]]*\]")


def strip_model_tokens(text: str) -> str:
    """Remove leaked model-specific control tokens from text."""
    if not text:
        return text
    text = _MODEL_SPECIAL_TOKEN_RE.sub("", text)
    text = _FULLWIDTH_SPECIAL_TOKEN_RE.sub("", text)
    text = _MINIMAX_INVOKE_RE.sub("", text)
    text = _DOWNGRADED_TOOL_RE.sub("", text)
    return text


def parse_stream_chunk(
    chunk: str,
    state: ThinkTagState,
) -> list[ParsedSegment]:
    """Parse a streaming chunk, tracking think tag state across calls.

    Returns a list of typed segments. State is mutated to track
    whether we're currently inside a thinking block.
    """
    if not chunk:
        return []

    # Prepend any buffered partial tag
    text = state._buffer + chunk
    state._buffer = ""

    # Buffer only when the tail looks like a partial think tag opening.
    # A bare "<" followed by end-of-chunk could be a normal comparison
    # operator, so only buffer when followed by "/" or a tag-name char.
    if len(text) >= 2 and text[-2] == "<" and text[-1] in ("/", "t", "T", "a", "A"):
        state._buffer = text[-2:]
        text = text[:-2]
        if not text:
            return []
    elif text.endswith("<"):
        # Lone "<" at end — peek-buffer just one char
        state._buffer = "<"
        text = text[:-1]
        if not text:
            return []

    segments: list[ParsedSegment] = []
    pos = 0

    while pos < len(text):
        if state.in_thinking:
            # Look for closing tag
            close_match = _THINK_CLOSE_RE.search(text, pos)
            if close_match:
                thinking_content = text[pos:close_match.start()]
                if thinking_content:
                    segments.append(ParsedSegment(SegmentType.THINKING, thinking_content))
                segments.append(ParsedSegment(SegmentType.THINKING_END, ""))
                state.in_thinking = False
                pos = close_match.end()
            else:
                # Still in thinking, emit rest as thinking content
                remaining = text[pos:]
                if remaining:
                    segments.append(ParsedSegment(SegmentType.THINKING, remaining))
                break
        else:
            # Look for opening tag
            open_match = _THINK_OPEN_RE.search(text, pos)
            if open_match:
                # Text before the tag
                before = text[pos:open_match.start()]
                if before:
                    segments.append(ParsedSegment(SegmentType.TEXT, strip_model_tokens(before)))
                state.in_thinking = True
                pos = open_match.end()
            else:
                # No more tags, emit rest as text
                remaining = text[pos:]
                if remaining:
                    segments.append(ParsedSegment(SegmentType.TEXT, strip_model_tokens(remaining)))
                break

    return segments
