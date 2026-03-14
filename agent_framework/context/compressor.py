from __future__ import annotations

import hashlib
import logging
from enum import Enum
from typing import Any, Callable

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message

logger = logging.getLogger(__name__)


class CompressionStrategy(str, Enum):
    TOOL_RESULT_SUMMARY = "TOOL_RESULT_SUMMARY"
    SLIDING_WINDOW = "SLIDING_WINDOW"
    LLM_SUMMARIZE = "LLM_SUMMARIZE"
    LLMLINGUA_COMPRESS = "LLMLINGUA_COMPRESS"


# Number of most-recent groups protected from compression
_PROTECTED_RECENT_GROUPS = 2


class ContextCompressor:
    """Compresses context when it exceeds budget.

    Compression boundary rules:
    - Only Session History participates in compression
    - Compression operates on ToolTransactionGroup units (never splits a group)
    - System Core, Saved Memories, Current Input NEVER enter the compressor
    - Compression results are used for THIS LLM call only
    - Results do NOT write back to SessionState or iteration_history

    Strategies:
    - SLIDING_WINDOW: drop oldest groups (fast, lossy)
    - TOOL_RESULT_SUMMARY: truncate long tool outputs, then sliding window
    - LLM_SUMMARIZE: call LLM to produce structured summary of old groups,
      keep recent groups intact (best quality, costs 1 extra LLM call)
    """

    def __init__(
        self,
        strategy: CompressionStrategy = CompressionStrategy.SLIDING_WINDOW,
        token_counter: Callable[[list[Message]], int] | None = None,
    ) -> None:
        self._strategy = strategy
        self._token_counter = token_counter or self._rough_count
        # Cache: hash of compressed messages → summary group
        self._summary_cache: dict[str, ToolTransactionGroup] = {}

    def compress_groups(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any = None,
    ) -> list[ToolTransactionGroup]:
        """Compress session groups to fit within target_tokens.

        Args:
            groups: Transaction groups from session history.
            target_tokens: Maximum token budget for session history slot.
            model_adapter: Required for LLM_SUMMARIZE strategy. If None,
                falls back to SLIDING_WINDOW.
        """
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
            if model_adapter is None:
                logger.warning("LLM_SUMMARIZE requires model_adapter, falling back to SLIDING_WINDOW")
                return self._sliding_window(groups, target_tokens)
            return self._llm_summarize_sync(groups, target_tokens, model_adapter)
        if self._strategy == CompressionStrategy.LLMLINGUA_COMPRESS:
            logger.warning("LLMLINGUA_COMPRESS not implemented, falling back to SLIDING_WINDOW")
            return self._sliding_window(groups, target_tokens)

        return self._sliding_window(groups, target_tokens)

    async def compress_groups_async(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any = None,
    ) -> list[ToolTransactionGroup]:
        """Async version — required for LLM_SUMMARIZE strategy."""
        current_tokens = sum(
            g.token_estimate or self._count_group(g) for g in groups
        )
        if current_tokens <= target_tokens:
            return groups

        if self._strategy == CompressionStrategy.LLM_SUMMARIZE and model_adapter:
            return await self._llm_summarize(groups, target_tokens, model_adapter)

        # Non-async strategies
        return self.compress_groups(groups, target_tokens, model_adapter)

    # ------------------------------------------------------------------
    # SLIDING_WINDOW
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # TOOL_RESULT_SUMMARY
    # ------------------------------------------------------------------

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

        for g in compressed:
            g.token_estimate = self._count_group(g)

        current = sum(g.token_estimate for g in compressed)
        if current <= target_tokens:
            return compressed

        return self._sliding_window(compressed, target_tokens)

    # ------------------------------------------------------------------
    # LLM_SUMMARIZE
    # ------------------------------------------------------------------

    async def _llm_summarize(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any,
    ) -> list[ToolTransactionGroup]:
        """Compress old groups into a structured summary via LLM call.

        Splits groups into:
        - old_groups: compressed into a single summary message
        - recent_groups: kept intact (last N groups protected)

        The summary replaces old_groups as a single PLAIN_MESSAGES group
        with role="user" and <context-summary> XML wrapper.
        """
        if len(groups) <= _PROTECTED_RECENT_GROUPS:
            # Not enough groups to split — fall back to sliding window
            return self._sliding_window(groups, target_tokens)

        # Split: old (compressible) vs recent (protected)
        split_point = max(1, len(groups) - _PROTECTED_RECENT_GROUPS)
        old_groups = groups[:split_point]
        recent_groups = groups[split_point:]

        # Check if recent alone fits
        recent_tokens = sum(
            g.token_estimate or self._count_group(g) for g in recent_groups
        )
        if recent_tokens > target_tokens:
            # Even recent doesn't fit — sliding window as last resort
            return self._sliding_window(groups, target_tokens)

        # Check cache
        cache_key = self._compute_cache_key(old_groups)
        if cache_key in self._summary_cache:
            summary_group = self._summary_cache[cache_key]
            logger.info("compression.cache_hit", cache_key=cache_key[:8])
            return [summary_group] + recent_groups

        # Build the text to compress
        lines: list[str] = []
        for g in old_groups:
            for msg in g.messages:
                role = msg.role
                content = msg.content or ""
                if msg.tool_calls:
                    tool_names = ", ".join(tc.function_name for tc in msg.tool_calls)
                    lines.append(f"[{role}] (calls: {tool_names})")
                elif msg.tool_call_id:
                    lines.append(f"[tool:{msg.name or '?'}] {content[:300]}")
                else:
                    lines.append(f"[{role}] {content[:500]}")
        history_text = "\n".join(lines)

        # Call LLM with compression prompt
        from agent_framework.agent.prompt_templates import CONTEXT_COMPRESSION_PROMPT

        compress_messages = [
            Message(role="system", content=CONTEXT_COMPRESSION_PROMPT),
            Message(role="user", content=f"以下是需要压缩的历史对话：\n\n{history_text}"),
        ]

        try:
            response = await model_adapter.complete(
                messages=compress_messages,
                temperature=0.0,
                max_tokens=1024,
            )
            summary_text = response.content or ""
        except Exception as e:
            logger.error("compression.llm_failed", error=str(e))
            return self._sliding_window(groups, target_tokens)

        if not summary_text.strip():
            return self._sliding_window(groups, target_tokens)

        # Wrap summary as a context-summary message
        summary_content = f"<context-summary>\n{summary_text.strip()}\n</context-summary>"
        summary_msg = Message(role="user", content=summary_content)

        summary_group = ToolTransactionGroup(
            group_id=f"summary_{cache_key[:8]}",
            group_type="PLAIN_MESSAGES",
            messages=[summary_msg],
            token_estimate=self._token_counter([summary_msg]),
            protected=True,  # Summary must not be further trimmed
        )

        # Verify total fits
        total = summary_group.token_estimate + recent_tokens
        if total > target_tokens:
            # Summary too large — fall back
            return self._sliding_window(groups, target_tokens)

        # Cache and return
        self._summary_cache[cache_key] = summary_group
        logger.info(
            "compression.llm_summarized",
            old_groups=len(old_groups),
            old_tokens=sum(g.token_estimate or self._count_group(g) for g in old_groups),
            summary_tokens=summary_group.token_estimate,
            recent_groups=len(recent_groups),
        )

        return [summary_group] + recent_groups

    def _llm_summarize_sync(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any,
    ) -> list[ToolTransactionGroup]:
        """Sync fallback for LLM_SUMMARIZE — uses sliding window.

        The actual LLM call is async. In sync compress_groups(),
        we fall back to sliding window. Use compress_groups_async()
        for the real LLM summarization.
        """
        logger.info("compression.llm_summarize_sync_fallback")
        return self._sliding_window(groups, target_tokens)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _compute_cache_key(groups: list[ToolTransactionGroup]) -> str:
        """Deterministic hash of groups for cache lookup."""
        parts = []
        for g in groups:
            for msg in g.messages:
                parts.append(f"{msg.role}:{(msg.content or '')[:100]}")
        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]
