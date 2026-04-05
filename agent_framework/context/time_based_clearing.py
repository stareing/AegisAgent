"""Time-based tool result clearing — expire old results when cache is cold.

When the gap since the last assistant message exceeds a threshold, the
provider-side KV cache has likely expired. Old tool results no longer
benefit from caching and only waste tokens. This module clears them
before compaction runs, using the lightest-weight approach first.

Aligns with Claude Code's maybeTimeBasedMicrocompact() pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agent_framework.context.tool_use_summary import CLEARED_MESSAGE, COMPACTABLE_TOOLS
from agent_framework.infra.logger import get_logger

if TYPE_CHECKING:
    from agent_framework.context.transaction_group import ToolTransactionGroup
    from agent_framework.models.message import Message

logger = get_logger(__name__)

DEFAULT_GAP_THRESHOLD_MINUTES = 5.0
DEFAULT_KEEP_RECENT = 3


class TimeBasedClearing:
    """Clears expired tool results based on time gap since last assistant turn.

    When the conversation has been idle longer than the threshold, the
    provider's KV cache is cold. Old tool result content becomes pure
    token waste — clear it to free context budget for new content.

    Only clears tools in COMPACTABLE_TOOLS. Keeps the N most recent
    tool results intact (they're likely still relevant).
    """

    def __init__(
        self,
        gap_threshold_minutes: float = DEFAULT_GAP_THRESHOLD_MINUTES,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ) -> None:
        self._threshold_minutes = gap_threshold_minutes
        self._keep_recent = max(1, keep_recent)

    def should_trigger(self, messages: list[Message]) -> bool:
        """Check if time-based clearing should activate.

        Looks for the last assistant message's metadata.timestamp.
        Returns True if the gap exceeds the configured threshold.
        """
        last_assistant_ts = self._find_last_assistant_timestamp(messages)
        if last_assistant_ts is None:
            return False

        now = datetime.now(timezone.utc)
        gap_minutes = (now - last_assistant_ts).total_seconds() / 60.0
        return gap_minutes >= self._threshold_minutes

    def clear_old_tool_results(
        self,
        groups: list[ToolTransactionGroup],
    ) -> list[ToolTransactionGroup]:
        """Clear old compactable tool results, keeping the N most recent.

        Scans all groups for role="tool" messages with compactable tool names.
        Keeps the most recent `keep_recent` tool results intact.
        Replaces older ones with CLEARED_MESSAGE.

        Returns a new list of groups (does not mutate originals).
        """
        from agent_framework.context.transaction_group import ToolTransactionGroup

        # Collect all compactable tool result positions: (group_idx, msg_idx)
        compactable_positions: list[tuple[int, int]] = []
        for gi, group in enumerate(groups):
            for mi, msg in enumerate(group.messages):
                if (
                    msg.role == "tool"
                    and msg.name in COMPACTABLE_TOOLS
                    and msg.content
                    and msg.content != CLEARED_MESSAGE
                ):
                    compactable_positions.append((gi, mi))

        if not compactable_positions:
            return groups

        # Keep the N most recent, clear the rest
        keep_set = set(compactable_positions[-self._keep_recent:])
        clear_set = set(compactable_positions) - keep_set

        if not clear_set:
            return groups

        # Build new groups with cleared content
        tokens_saved_chars = 0
        modified_groups: dict[int, list] = {}

        for gi, mi in clear_set:
            if gi not in modified_groups:
                modified_groups[gi] = list(groups[gi].messages)
            msg = modified_groups[gi][mi]
            tokens_saved_chars += len(msg.content or "")
            modified_groups[gi][mi] = msg.model_copy(
                update={"content": CLEARED_MESSAGE}
            )

        result: list[ToolTransactionGroup] = []
        for gi, group in enumerate(groups):
            if gi in modified_groups:
                result.append(ToolTransactionGroup(
                    group_id=group.group_id,
                    group_type=group.group_type,
                    messages=modified_groups[gi],
                    token_estimate=0,  # Will be recalculated
                    protected=group.protected,
                ))
            else:
                result.append(group)

        logger.info(
            "time_based_clearing.applied",
            cleared_count=len(clear_set),
            kept_count=len(keep_set),
            chars_freed=tokens_saved_chars,
        )
        return result

    @staticmethod
    def _find_last_assistant_timestamp(
        messages: list[Message],
    ) -> datetime | None:
        """Find the timestamp of the last assistant message from metadata."""
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.metadata:
                ts_str = msg.metadata.get("timestamp")
                if ts_str:
                    try:
                        return datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue
        return None
