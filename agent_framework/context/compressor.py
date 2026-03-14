from __future__ import annotations

import logging
from enum import Enum
from typing import Callable

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message

logger = logging.getLogger(__name__)


class CompressionStrategy(str, Enum):
    TOOL_RESULT_SUMMARY = "TOOL_RESULT_SUMMARY"
    SLIDING_WINDOW = "SLIDING_WINDOW"
    LLM_SUMMARIZE = "LLM_SUMMARIZE"
    LLMLINGUA_COMPRESS = "LLMLINGUA_COMPRESS"


class ContextCompressor:
    """Compresses context when it exceeds budget.

    Rules (section 12.6):
    1. First trim session history
    2. Then compress long tool results
    3. Then summarize early history
    4. Saved Memories are NOT lossy-compressed by default
    """

    def __init__(
        self,
        strategy: CompressionStrategy = CompressionStrategy.SLIDING_WINDOW,
        token_counter: Callable[[list[Message]], int] | None = None,
    ) -> None:
        self._strategy = strategy
        self._token_counter = token_counter or self._rough_count

    def compress_groups(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
    ) -> list[ToolTransactionGroup]:
        """Compress session groups to fit within target_tokens."""
        current_tokens = sum(
            g.token_estimate or self._count_group(g) for g in groups
        )
        if current_tokens <= target_tokens:
            return groups

        if self._strategy == CompressionStrategy.SLIDING_WINDOW:
            return self._sliding_window(groups, target_tokens)
        if self._strategy == CompressionStrategy.TOOL_RESULT_SUMMARY:
            return self._tool_result_summary(groups, target_tokens)
        if self._strategy == CompressionStrategy.LLM_SUMMARIZE:
            logger.warning(
                "LLM_SUMMARIZE compression strategy is not yet implemented, "
                "falling back to SLIDING_WINDOW"
            )
            return self._sliding_window(groups, target_tokens)
        if self._strategy == CompressionStrategy.LLMLINGUA_COMPRESS:
            logger.warning(
                "LLMLINGUA_COMPRESS compression strategy is not yet implemented, "
                "falling back to SLIDING_WINDOW"
            )
            return self._sliding_window(groups, target_tokens)

        return self._sliding_window(groups, target_tokens)

    def _sliding_window(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
    ) -> list[ToolTransactionGroup]:
        """Keep only the most recent groups that fit."""
        result: list[ToolTransactionGroup] = []
        total = 0
        for g in reversed(groups):
            est = g.token_estimate or self._count_group(g)
            if total + est > target_tokens and not g.protected:
                continue
            result.insert(0, g)
            total += est
        return result

    def _tool_result_summary(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
    ) -> list[ToolTransactionGroup]:
        """Summarize long tool results to reduce token usage."""
        compressed = []
        max_tool_output = 200  # chars

        for g in groups:
            if g.group_type in ("TOOL_BATCH", "SUBAGENT_BATCH"):
                new_msgs = []
                for msg in g.messages:
                    if msg.role == "tool" and msg.content and len(msg.content) > max_tool_output:
                        truncated = msg.content[:max_tool_output] + "\n... [truncated]"
                        new_msgs.append(msg.model_copy(update={"content": truncated}))
                    else:
                        new_msgs.append(msg)
                g = g.model_copy(update={"messages": new_msgs, "token_estimate": 0})
            compressed.append(g)

        # Recalculate and check if we fit now
        for g in compressed:
            g.token_estimate = self._count_group(g)

        current = sum(g.token_estimate for g in compressed)
        if current <= target_tokens:
            return compressed

        # Still too large - fall back to sliding window
        return self._sliding_window(compressed, target_tokens)

    def _count_group(self, group: ToolTransactionGroup) -> int:
        return self._token_counter(group.messages)

    @staticmethod
    def _rough_count(messages: list[Message]) -> int:
        total = 0
        for m in messages:
            if m.content:
                total += len(m.content) // 4
            if m.tool_calls:
                total += len(str(m.tool_calls)) // 4
        return max(total, 1)
