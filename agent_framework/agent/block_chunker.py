"""Block chunker — fence-aware text chunking for streaming output.

Splits streaming text into well-formed chunks respecting:
- Markdown code fence boundaries (never splits inside ```)
- Paragraph boundaries (preferred break point)
- Newline and sentence boundaries (fallback)
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class BlockChunkConfig(BaseModel):
    """Configuration for block chunking."""

    model_config = {"frozen": True}

    min_chars: int = 100
    max_chars: int = 2000
    break_preference: str = "paragraph"  # "paragraph" | "newline" | "sentence"
    flush_on_paragraph: bool = True


# Fence detection (``` with optional language tag)
_FENCE_RE = re.compile(r"^(`{3,})", re.MULTILINE)
_PARAGRAPH_BREAK = "\n\n"
_SENTENCE_END_RE = re.compile(r"[.!?]\s+")


def _count_open_fences(text: str) -> bool:
    """Check if text has an unclosed code fence."""
    count = 0
    for _match in _FENCE_RE.finditer(text):
        count += 1
    return count % 2 == 1


def _find_safe_break(text: str, start: int, end: int, preference: str) -> int:
    """Find the best break point in text[start:end] that doesn't split a fence.

    Returns the break position (absolute index), or -1 if no safe break found.
    """
    region = text[start:end]

    # Never break inside a code fence
    if _count_open_fences(text[:end]):
        # We're inside a fence — look for fence close first
        fence_close = region.rfind("```")
        if fence_close > 0:
            # Break after the fence close line
            newline_after = region.find("\n", fence_close + 3)
            if newline_after >= 0:
                return start + newline_after + 1
        return -1  # Can't break safely

    # Try preferred break point
    if preference == "paragraph":
        idx = region.rfind(_PARAGRAPH_BREAK)
        if idx >= 0:
            return start + idx + len(_PARAGRAPH_BREAK)

    # Fallback: newline
    idx = region.rfind("\n")
    if idx >= 0:
        return start + idx + 1

    # Fallback: sentence end
    matches = list(_SENTENCE_END_RE.finditer(region))
    if matches:
        last = matches[-1]
        return start + last.end()

    return -1


class BlockChunker:
    """Stateful text chunker that respects markdown fences and paragraph boundaries."""

    def __init__(self, config: BlockChunkConfig | None = None) -> None:
        self._config = config or BlockChunkConfig()
        self._buffer: str = ""

    def add(self, text: str) -> list[str]:
        """Add text and return any complete chunks."""
        self._buffer += text
        return self._flush_ready()

    def flush_all(self) -> list[str]:
        """Flush all remaining buffered text as chunks."""
        if not self._buffer:
            return []
        chunks = self._flush_ready()
        if self._buffer:
            chunks.append(self._buffer)
            self._buffer = ""
        return chunks

    def _flush_ready(self) -> list[str]:
        """Extract ready chunks from the buffer."""
        config = self._config
        chunks: list[str] = []

        while len(self._buffer) >= config.min_chars:
            # Check for paragraph flush
            if config.flush_on_paragraph and len(self._buffer) >= config.min_chars:
                para_idx = self._buffer.find(_PARAGRAPH_BREAK, config.min_chars // 2)
                if 0 < para_idx < config.max_chars:
                    # Check we're not inside a fence
                    if not _count_open_fences(self._buffer[:para_idx]):
                        break_at = para_idx + len(_PARAGRAPH_BREAK)
                        chunks.append(self._buffer[:break_at])
                        self._buffer = self._buffer[break_at:]
                        continue

            # Buffer exceeds max — must break
            if len(self._buffer) > config.max_chars:
                break_at = _find_safe_break(
                    self._buffer,
                    config.min_chars,
                    config.max_chars,
                    config.break_preference,
                )
                if break_at > 0:
                    chunks.append(self._buffer[:break_at])
                    self._buffer = self._buffer[break_at:]
                else:
                    # No safe break — force at max_chars
                    chunks.append(self._buffer[: config.max_chars])
                    self._buffer = self._buffer[config.max_chars :]
            else:
                break  # Buffer not big enough to flush

        return chunks

    def reset(self) -> None:
        """Clear the buffer."""
        self._buffer = ""
