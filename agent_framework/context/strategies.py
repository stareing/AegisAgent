"""Compression strategy enum and error types for context engineering.

Controls how session history is reduced when token budget is exceeded.
"""

from __future__ import annotations

from enum import Enum


class CompressionStrategy(str, Enum):
    """Strategy for compressing session history when over budget.

    SUMMARIZATION: LLM-based incremental summarization (default).
    TRUNCATION: Drop oldest groups, keep most recent N.
    HYBRID: Summarize old groups, keep recent N verbatim.
    NONE: Never compress — fail if budget exceeded.
    """

    SUMMARIZATION = "SUMMARIZATION"
    TRUNCATION = "TRUNCATION"
    HYBRID = "HYBRID"
    NONE = "NONE"

    # Legacy alias
    LLM_SUMMARIZE = "SUMMARIZATION"


class ContextBudgetExceeded(Exception):
    """Raised when context exceeds token budget after compression (spec §8.1)."""

    def __init__(self, total_tokens: int, budget_tokens: int, strategy_used: str):
        self.total_tokens = total_tokens
        self.budget_tokens = budget_tokens
        self.strategy_used = strategy_used
        super().__init__(
            f"Context budget exceeded: {total_tokens}/{budget_tokens} tokens "
            f"(strategy={strategy_used})"
        )


class CompressionError(Exception):
    """Raised when compression fails (spec §8.2). Triggers TRUNCATION fallback."""

    def __init__(self, strategy: str, original_error: str):
        self.strategy = strategy
        self.original_error = original_error
        super().__init__(f"Compression failed (strategy={strategy}): {original_error}")
