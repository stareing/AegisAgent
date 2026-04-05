"""Reusable async retry with exponential backoff and jitter.

Used by compaction, LLM calls, and other transient-failure-prone operations.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class RetryConfig(BaseModel):
    """Configuration for retry behavior."""

    model_config = {"frozen": True}

    max_attempts: int = 3
    min_delay_ms: int = 500
    max_delay_ms: int = 5000
    jitter: float = 0.2  # 0.0 - 1.0
    label: str = ""


def _compute_delay(attempt: int, config: RetryConfig) -> float:
    """Compute delay in seconds with exponential backoff and jitter."""
    # Exponential: min_delay * 2^attempt, capped at max_delay
    base_ms = config.min_delay_ms * (2 ** attempt)
    capped_ms = min(base_ms, config.max_delay_ms)

    # Apply jitter
    if config.jitter > 0:
        jitter_range = capped_ms * config.jitter
        capped_ms += random.uniform(-jitter_range, jitter_range)

    return max(config.min_delay_ms, capped_ms) / 1000.0


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    config: RetryConfig | None = None,
    should_retry: Callable[[Exception, int], bool] | None = None,
) -> T:
    """Execute an async function with retry on failure.

    Args:
        fn: Async callable to retry.
        config: Retry configuration.
        should_retry: Optional predicate(error, attempt) -> bool.
            If provided, only retries when it returns True.

    Returns:
        The result of fn() on success.

    Raises:
        The last exception if all attempts fail.
    """
    if config is None:
        config = RetryConfig()

    label = config.label or "retry"
    last_error: Exception | None = None

    for attempt in range(config.max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_error = e
            remaining = config.max_attempts - attempt - 1

            if remaining <= 0:
                break

            # Check if we should retry this error
            if should_retry and not should_retry(e, attempt):
                break

            delay = _compute_delay(attempt, config)
            logger.info(
                "retry.attempt_failed",
                label=label,
                attempt=attempt + 1,
                remaining=remaining,
                delay_s=round(delay, 2),
                error=str(e)[:200],
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
