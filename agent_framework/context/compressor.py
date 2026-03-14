"""Context compressor — manages session history compression with incremental summarization.

Compression boundary rules:
- Only Session History participates in compression
- Compression operates on ToolTransactionGroup units (never splits a group)
- System Core, Saved Memories, Current Input NEVER enter the compressor
- User input can trigger budget check, but user input itself is never compressed

Incremental compression model:
- Maintains a frozen summary block covering already-compressed history
- New groups accumulate in the "uncovered" zone
- Compression only triggers when uncovered zone exceeds budget
- Frozen summary is reused across rounds until invalidated
- Three-segment assembly: [frozen_summary] + [recent_detail] + [current_input]
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message

logger = logging.getLogger(__name__)


class CompressionStrategy(str, Enum):
    TOOL_RESULT_SUMMARY = "TOOL_RESULT_SUMMARY"
    SLIDING_WINDOW = "SLIDING_WINDOW"
    LLM_SUMMARIZE = "LLM_SUMMARIZE"
    LLMLINGUA_COMPRESS = "LLMLINGUA_COMPRESS"


class SummaryBlock(BaseModel):
    """Persistent summary block covering a range of compressed history.

    Reused across rounds until invalidated by new compression.
    """

    summary_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    covered_group_count: int = 0
    source_hash: str = ""
    summary_version: int = 1
    summary_text: str = ""
    token_estimate: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Number of most-recent groups protected from compression
_PROTECTED_RECENT_GROUPS = 2


class ContextCompressor:
    """Compresses session history with incremental LLM summarization.

    Three-segment assembly:
    1. Frozen summary block (covers already-compressed old history)
    2. Recent detail zone (last N groups, kept intact)
    3. Current input (never enters compressor)

    Incremental rules:
    - Frozen summary reused if no new groups need compression
    - Only uncovered groups (between frozen summary and recent zone) are compressed
    - New compression extends the frozen summary, doesn't rebuild from scratch
    """

    def __init__(
        self,
        strategy: CompressionStrategy = CompressionStrategy.SLIDING_WINDOW,
        token_counter: Callable[[list[Message]], int] | None = None,
    ) -> None:
        self._strategy = strategy
        self._token_counter = token_counter or self._rough_count
        # Persistent frozen summary — survives across rounds within a run
        self._frozen_summary: SummaryBlock | None = None
        self._frozen_summary_group_count: int = 0  # how many groups are covered

    def compress_groups(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any = None,
    ) -> list[ToolTransactionGroup]:
        """Synchronous compression (SLIDING_WINDOW / TOOL_RESULT_SUMMARY)."""
        current_tokens = sum(
            g.token_estimate or self._count_group(g) for g in groups
        )
        if current_tokens <= target_tokens:
            return self._prepend_frozen_summary(groups, target_tokens)

        if self._strategy == CompressionStrategy.SLIDING_WINDOW:
            return self._sliding_window(groups, target_tokens)
        if self._strategy == CompressionStrategy.TOOL_RESULT_SUMMARY:
            return self._tool_result_summary(groups, target_tokens)
        if self._strategy in (CompressionStrategy.LLM_SUMMARIZE, CompressionStrategy.LLMLINGUA_COMPRESS):
            if model_adapter is None:
                logger.info("compression.llm_strategy_sync_fallback")
                return self._sliding_window(groups, target_tokens)
            return self._sliding_window(groups, target_tokens)

        return self._sliding_window(groups, target_tokens)

    async def compress_groups_async(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any = None,
    ) -> list[ToolTransactionGroup]:
        """Async compression — required for LLM_SUMMARIZE."""
        current_tokens = sum(
            g.token_estimate or self._count_group(g) for g in groups
        )
        if current_tokens <= target_tokens:
            return self._prepend_frozen_summary(groups, target_tokens)

        if self._strategy == CompressionStrategy.LLM_SUMMARIZE and model_adapter:
            return await self._llm_summarize(groups, target_tokens, model_adapter)

        return self.compress_groups(groups, target_tokens, model_adapter)

    # ------------------------------------------------------------------
    # Frozen summary management
    # ------------------------------------------------------------------

    def _prepend_frozen_summary(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int = 0,
    ) -> list[ToolTransactionGroup]:
        """If we have a frozen summary and groups don't include it yet, prepend it.

        Post-prepend budget check: if adding the summary exceeds target_tokens,
        drop the summary and return groups as-is (prevents token explosion).
        """
        if not self._frozen_summary:
            return groups
        if groups and groups[0].group_id.startswith("summary_"):
            return groups
        summary_group = self._build_summary_group(self._frozen_summary)
        result = [summary_group] + groups

        # Budget guard: verify prepending didn't blow the budget
        if target_tokens > 0:
            total = sum(g.token_estimate or self._count_group(g) for g in result)
            if total > target_tokens:
                logger.warning(
                    "compression.prepend_exceeds_budget total=%d budget=%d",
                    total, target_tokens,
                )
                return groups  # Drop summary to stay in budget

        return result

    def _build_summary_group(self, block: SummaryBlock) -> ToolTransactionGroup:
        """Convert SummaryBlock to a ToolTransactionGroup."""
        content = f"<context-summary version=\"{block.summary_version}\">\n{block.summary_text}\n</context-summary>"
        msg = Message(role="user", content=content)
        return ToolTransactionGroup(
            group_id=f"summary_{block.summary_id}",
            group_type="PLAIN_MESSAGES",
            messages=[msg],
            token_estimate=block.token_estimate or self._token_counter([msg]),
            protected=True,
        )

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
        """Truncate long tool results, then sliding window if needed."""
        compressed = []
        max_tool_output = 200

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
    # LLM_SUMMARIZE — incremental
    # ------------------------------------------------------------------

    async def _llm_summarize(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any,
    ) -> list[ToolTransactionGroup]:
        """Incremental LLM compression with frozen summary reuse.

        Logic:
        1. If frozen summary exists and covers some groups, skip those
        2. Identify uncovered groups that need compression
        3. If uncovered zone is small enough, just use sliding window
        4. Otherwise, call LLM to compress uncovered zone
        5. Merge new summary with existing frozen summary
        6. Return: [merged_summary] + [recent_detail]
        """
        if len(groups) <= _PROTECTED_RECENT_GROUPS:
            return self._sliding_window(groups, target_tokens)

        # Split: old (compressible) vs recent (protected)
        split_point = max(1, len(groups) - _PROTECTED_RECENT_GROUPS)
        old_groups = groups[:split_point]
        recent_groups = groups[split_point:]

        recent_tokens = sum(
            g.token_estimate or self._count_group(g) for g in recent_groups
        )
        if recent_tokens > target_tokens:
            return self._sliding_window(groups, target_tokens)

        summary_budget = target_tokens - recent_tokens

        # Check if frozen summary already covers all old groups
        # Validate both count AND content hash to prevent stale reuse
        current_source_hash = self._compute_cache_key(old_groups)
        if (self._frozen_summary
                and self._frozen_summary_group_count >= len(old_groups)
                and self._frozen_summary.source_hash == current_source_hash):
            # Frozen summary covers everything with matching content — reuse
            summary_group = self._build_summary_group(self._frozen_summary)
            if summary_group.token_estimate <= summary_budget:
                logger.info("compression.frozen_reuse",
                            covered=self._frozen_summary_group_count,
                            hash=current_source_hash[:8])
                return [summary_group] + recent_groups

        # Determine uncovered groups (new since last compression)
        uncovered_start = self._frozen_summary_group_count if self._frozen_summary else 0
        uncovered_groups = old_groups[uncovered_start:]

        if not uncovered_groups:
            # Nothing new to compress — reuse frozen or sliding window
            if self._frozen_summary:
                summary_group = self._build_summary_group(self._frozen_summary)
                total = summary_group.token_estimate + recent_tokens
                if total <= target_tokens:
                    return [summary_group] + recent_groups
                # Summary + recent exceeds budget — fall back
                return self._sliding_window(groups, target_tokens)
            return self._sliding_window(groups, target_tokens)

        # Build full text to compress (frozen summary + uncovered)
        lines: list[str] = []

        # Include existing summary as context for the new compression
        if self._frozen_summary:
            lines.append(f"[previous summary]\n{self._frozen_summary.summary_text}")
            lines.append("")

        # Add uncovered groups — full content, no truncation
        for g in uncovered_groups:
            for msg in g.messages:
                role = msg.role
                content = msg.content or ""
                if msg.tool_calls:
                    import json
                    tool_names = ", ".join(tc.function_name for tc in msg.tool_calls)
                    args_preview = ", ".join(
                        f"{tc.function_name}({json.dumps(tc.arguments, ensure_ascii=False, default=str)})"
                        for tc in msg.tool_calls
                    )
                    lines.append(f"[{role}] (tool_calls: {args_preview})")
                elif msg.tool_call_id:
                    lines.append(f"[tool:{msg.name or '?'}] {content}")
                else:
                    lines.append(f"[{role}] {content}")

        history_text = "\n".join(lines)

        # Call LLM
        from agent_framework.agent.prompt_templates import CONTEXT_COMPRESSION_PROMPT

        compress_messages = [
            Message(role="system", content=CONTEXT_COMPRESSION_PROMPT),
            Message(role="user", content=f"以下是需要压缩的历史对话：\n\n{history_text}"),
        ]

        try:
            response = await model_adapter.complete(
                messages=compress_messages,
                temperature=0.0,
                max_tokens=2048,
            )
            summary_text = response.content or ""
        except Exception as e:
            logger.error("compression.llm_failed", error=str(e))
            return self._sliding_window(groups, target_tokens)

        if not summary_text.strip():
            return self._sliding_window(groups, target_tokens)

        # Create new frozen summary block
        source_hash = self._compute_cache_key(old_groups)
        new_version = (self._frozen_summary.summary_version + 1) if self._frozen_summary else 1

        new_summary = SummaryBlock(
            covered_group_count=len(old_groups),
            source_hash=source_hash,
            summary_version=new_version,
            summary_text=summary_text.strip(),
        )

        summary_group = self._build_summary_group(new_summary)
        new_summary.token_estimate = summary_group.token_estimate

        # Verify fits
        if summary_group.token_estimate + recent_tokens > target_tokens:
            logger.warning("compression.summary_too_large",
                           summary_tokens=summary_group.token_estimate,
                           budget=summary_budget)
            return self._sliding_window(groups, target_tokens)

        # Freeze the new summary
        self._frozen_summary = new_summary
        self._frozen_summary_group_count = len(old_groups)

        logger.info(
            "compression.llm_summarized",
            version=new_version,
            covered_groups=len(old_groups),
            uncovered_compressed=len(uncovered_groups),
            old_tokens=sum(g.token_estimate or self._count_group(g) for g in old_groups),
            summary_tokens=summary_group.token_estimate,
            recent_groups=len(recent_groups),
        )

        return [summary_group] + recent_groups

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
        parts = []
        for g in groups:
            for msg in g.messages:
                parts.append(f"{msg.role}:{(msg.content or '')[:100]}")
        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def reset(self) -> None:
        """Reset frozen summary — called at run start."""
        self._frozen_summary = None
        self._frozen_summary_group_count = 0
