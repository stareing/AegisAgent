"""Context compressor — LLM-based incremental summarization only.

Compression boundary rules:
- Only Session History participates in compression
- Compression operates on ToolTransactionGroup units (never splits a group)
- System Core, Saved Memories are NEVER compressed

Incremental compression model:
- Maintains a frozen summary block covering already-compressed history
- New groups accumulate in the "uncovered" zone
- Compression only triggers when total session tokens exceed budget
- Frozen summary is reused across rounds until invalidated
- Two-segment assembly: [frozen_summary] + [recent_detail]
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from agent_framework.context.identifier_preservation import (
    build_preservation_instructions,
    extract_identifiers,
)
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import ContentPart, Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adaptive compaction (OC-style)
# ---------------------------------------------------------------------------

class AdaptiveCompactionConfig(BaseModel):
    """Controls adaptive compression ratio based on message characteristics."""

    model_config = {"frozen": True}

    base_ratio: float = 0.4
    safety_margin: float = 1.2
    min_ratio: float = 0.15
    max_ratio: float = 0.8


def compute_adaptive_ratio(
    message_count: int,
    avg_message_tokens: int,
    context_window: int,
    config: AdaptiveCompactionConfig | None = None,
) -> float:
    """Compute adaptive compression ratio based on message size vs context window.

    When messages are large relative to context, use a smaller ratio
    (more aggressive compression). When messages are small, use the base ratio.
    """
    if config is None:
        config = AdaptiveCompactionConfig()

    if context_window <= 0 or message_count <= 0:
        return config.base_ratio

    # If average message is > 10% of context, reduce ratio
    message_fraction = avg_message_tokens / context_window
    if message_fraction > 0.1:
        scale = max(0.0, 1.0 - (message_fraction - 0.1) * 5)
        ratio = config.min_ratio + (config.base_ratio - config.min_ratio) * scale
    else:
        ratio = config.base_ratio

    return max(config.min_ratio, min(config.max_ratio, ratio))


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
    """Compresses session history via incremental LLM summarization.

    Two-segment assembly:
    1. Frozen summary block (covers already-compressed old history)
    2. Recent detail zone (last N groups, kept intact)

    Incremental rules:
    - Frozen summary reused if no new groups need compression
    - Only uncovered groups (between frozen summary and recent zone) are compressed
    - New compression extends the frozen summary, doesn't rebuild from scratch
    """

    def __init__(
        self,
        token_counter: Callable[[list[Message]], int] | None = None,
        strategy: str = "SUMMARIZATION",
    ) -> None:
        from agent_framework.context.strategies import CompressionStrategy
        self._token_counter = token_counter or self._rough_count
        self._strategy = CompressionStrategy(strategy) if isinstance(strategy, str) else strategy
        # Persistent frozen summary — survives across rounds within a run
        self._frozen_summary: SummaryBlock | None = None
        self._frozen_summary_group_count: int = 0

    async def compress_groups_async(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any = None,
    ) -> list[ToolTransactionGroup]:
        """Compress session groups using the configured strategy.

        Strategies:
        - SUMMARIZATION: LLM incremental summarization (default)
        - TRUNCATION: Drop oldest groups, keep recent N
        - HYBRID: Summarize old, keep recent verbatim
        - NONE: No compression (return as-is)

        On LLM failure, falls back to TRUNCATION (CE-013).
        """
        from agent_framework.context.strategies import CompressionStrategy

        current_tokens = sum(
            g.token_estimate or self._count_group(g) for g in groups
        )
        if current_tokens <= target_tokens:
            return self._prepend_frozen_summary(groups, target_tokens)

        if self._strategy == CompressionStrategy.NONE:
            return groups

        if self._strategy == CompressionStrategy.TRUNCATION:
            return self._truncate_groups(groups, target_tokens)

        # SUMMARIZATION or HYBRID — need model_adapter
        if not model_adapter:
            logger.warning("compression.no_adapter — falling back to truncation")
            return self._truncate_groups(groups, target_tokens)

        try:
            if self._strategy == CompressionStrategy.HYBRID:
                return await self._hybrid_compress(groups, target_tokens, model_adapter)
            # Default: SUMMARIZATION
            return await self._llm_summarize(groups, target_tokens, model_adapter)
        except Exception as exc:
            # CE-013: LLM failure → fallback to TRUNCATION
            logger.warning("compression.llm_failed_fallback_truncation",
                           strategy=self._strategy.value, error=str(exc))
            return self._truncate_groups(groups, target_tokens)

    def _truncate_groups(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
    ) -> list[ToolTransactionGroup]:
        """Drop oldest groups until within budget. Protects last N groups."""
        if not groups:
            return groups

        protected = groups[-_PROTECTED_RECENT_GROUPS:]
        trimmable = groups[:-_PROTECTED_RECENT_GROUPS] if len(groups) > _PROTECTED_RECENT_GROUPS else []

        while trimmable:
            total = sum(g.token_estimate or self._count_group(g) for g in trimmable + protected)
            if total <= target_tokens:
                break
            trimmable.pop(0)

        return trimmable + protected

    async def _hybrid_compress(
        self,
        groups: list[ToolTransactionGroup],
        target_tokens: int,
        model_adapter: Any,
    ) -> list[ToolTransactionGroup]:
        """Summarize old groups, keep recent N verbatim."""
        protected = groups[-_PROTECTED_RECENT_GROUPS:]
        old_groups = groups[:-_PROTECTED_RECENT_GROUPS] if len(groups) > _PROTECTED_RECENT_GROUPS else []

        if not old_groups:
            return groups

        # Summarize old groups
        summarized = await self._llm_summarize(old_groups, target_tokens // 2, model_adapter)
        return summarized + protected

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
        drop the summary and return groups as-is.
        """
        if not self._frozen_summary:
            return groups
        if groups and groups[0].group_id.startswith("summary_"):
            return groups
        summary_group = self._build_summary_group(self._frozen_summary)
        result = [summary_group] + groups

        if target_tokens > 0:
            total = sum(g.token_estimate or self._count_group(g) for g in result)
            if total > target_tokens:
                logger.warning(
                    "compression.prepend_exceeds_budget total=%d budget=%d",
                    total, target_tokens,
                )
                return groups

        return result

    def _build_summary_group(self, block: SummaryBlock) -> ToolTransactionGroup:
        """Convert SummaryBlock to a ToolTransactionGroup."""
        from agent_framework.context.summarizer import wrap_summary
        content = wrap_summary(block.summary_text, version=block.summary_version)
        msg = Message(role="user", content=content)
        return ToolTransactionGroup(
            group_id=f"summary_{block.summary_id}",
            group_type="PLAIN_MESSAGES",
            messages=[msg],
            token_estimate=block.token_estimate or self._token_counter([msg]),
            protected=True,
        )

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
        3. Call LLM to compress uncovered zone
        4. Merge new summary with existing frozen summary
        5. Return: [merged_summary] + [recent_detail]
        """
        if len(groups) <= _PROTECTED_RECENT_GROUPS:
            # Too few groups to compress — return as-is
            return groups

        # Protect groups containing multimodal content from compression.
        # Images/audio cannot be reconstructed from a text summary.
        for g in groups:
            if any(msg.has_multimodal for msg in g.messages):
                g.protected = True

        # Split: old (compressible) vs recent (protected)
        split_point = max(1, len(groups) - _PROTECTED_RECENT_GROUPS)
        old_groups = groups[:split_point]
        recent_groups = groups[split_point:]

        recent_tokens = sum(
            g.token_estimate or self._count_group(g) for g in recent_groups
        )
        if recent_tokens > target_tokens:
            # Even recent groups alone exceed budget — nothing we can do
            return groups

        summary_budget = target_tokens - recent_tokens

        # Check if frozen summary already covers all old groups
        current_source_hash = self._compute_cache_key(old_groups)
        frozen_hash_valid = (
            self._frozen_summary is not None
            and self._frozen_summary.source_hash == current_source_hash
        )

        if (frozen_hash_valid
                and self._frozen_summary_group_count >= len(old_groups)):
            summary_group = self._build_summary_group(self._frozen_summary)
            if summary_group.token_estimate <= summary_budget:
                logger.info(
                    "compression.frozen_reuse covered=%d hash=%s",
                    self._frozen_summary_group_count,
                    current_source_hash[:8],
                )
                return [summary_group] + recent_groups

        # If frozen summary exists but hash doesn't match, invalidate
        if self._frozen_summary and not frozen_hash_valid:
            logger.info(
                "compression.frozen_invalidated old_hash=%s new_hash=%s",
                self._frozen_summary.source_hash[:8],
                current_source_hash[:8],
            )
            self._frozen_summary = None
            self._frozen_summary_group_count = 0

        # Determine uncovered groups (new since last compression)
        uncovered_start = self._frozen_summary_group_count if self._frozen_summary else 0
        uncovered_groups = old_groups[uncovered_start:]

        if not uncovered_groups:
            if self._frozen_summary:
                summary_group = self._build_summary_group(self._frozen_summary)
                total = summary_group.token_estimate + recent_tokens
                if total <= target_tokens:
                    return [summary_group] + recent_groups
            return groups

        # Build text and call LLM via shared summarizer
        from agent_framework.context.summarizer import (call_llm_compress,
                                                        messages_to_text)

        uncovered_msgs = [msg for g in uncovered_groups for msg in g.messages]
        history_text = messages_to_text(uncovered_msgs)
        previous_summary = self._frozen_summary.summary_text if self._frozen_summary else None

        # Identifier preservation: extract IDs before compression and inject
        # "MUST preserve" instructions so the LLM keeps them in the summary
        identifiers = extract_identifiers(history_text)
        preservation_addon = build_preservation_instructions(identifiers)

        from agent_framework.infra.retry import RetryConfig, retry_async

        async def _do_compress() -> str:
            result = await call_llm_compress(
                history_text, model_adapter,
                previous_summary=previous_summary,
                extra_instructions=preservation_addon,
            )
            if not result:
                raise RuntimeError("LLM compression returned empty result")
            return result

        try:
            summary_text = await retry_async(
                _do_compress,
                config=RetryConfig(max_attempts=3, min_delay_ms=500, max_delay_ms=5000, label="compaction"),
            )
        except (RuntimeError, Exception):
            summary_text = None

        if not summary_text:
            # LLM failed — return groups as-is rather than lossy fallback
            logger.warning("compression.llm_failed — returning groups as-is")
            return groups

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

        if summary_group.token_estimate + recent_tokens > target_tokens:
            logger.warning(
                "compression.summary_too_large tokens=%d budget=%d",
                summary_group.token_estimate, summary_budget,
            )
            return groups

        # Freeze the new summary
        self._frozen_summary = new_summary
        self._frozen_summary_group_count = len(old_groups)

        old_tokens = sum(g.token_estimate or self._count_group(g) for g in old_groups)
        logger.info(
            "compression.llm_summarized v=%d covered=%d uncovered=%d old_tok=%d sum_tok=%d recent=%d",
            new_version, len(old_groups), len(uncovered_groups),
            old_tokens, summary_group.token_estimate, len(recent_groups),
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
            text = m.text_content
            if text:
                total += len(text) // 4
            if m.tool_calls:
                total += len(str(m.tool_calls)) // 4
            # Multimodal parts: estimate ~85 tokens per image, ~50 per audio chunk
            if m.content_parts:
                for p in m.content_parts:
                    if p.type == "text":
                        continue
                    if p.data:
                        total += len(p.data) // 4
                    else:
                        total += 85  # URL reference estimate
        return max(total, 1)

    @staticmethod
    def _compute_cache_key(groups: list[ToolTransactionGroup]) -> str:
        parts = []
        for g in groups:
            for msg in g.messages:
                text = msg.text_content or ""
                parts.append(f"{msg.role}:{text[:100]}")
        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def reset(self) -> None:
        """Reset frozen summary — called at run start."""
        self._frozen_summary = None
        self._frozen_summary_group_count = 0
