"""Dynamic concurrency controller for sub-agent pool auto-scaling.

Adjusts the concurrency limit based on current load:
- Scales up when utilization exceeds scale_up_threshold
- Scales down after sustained idle period below scale_down_threshold
"""

from __future__ import annotations

import asyncio
import time

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Minimum idle duration (seconds) before scaling down
_SCALE_DOWN_IDLE_SECONDS: float = 5.0


class DynamicConcurrencyController:
    """Semaphore-like primitive with a dynamically adjustable concurrency limit.

    Constructor parameters:
        min_concurrent: Floor for the concurrency limit.
        max_concurrent: Ceiling for the concurrency limit.
        scale_up_threshold: When running_count / current_limit >= this value,
            the limit is increased by 1 (up to *max_concurrent*).
        scale_down_threshold: When running_count / current_limit <= this value
            and the system has been idle for >= 5 seconds, the limit is
            decreased by 1 (down to *min_concurrent*).
    """

    def __init__(
        self,
        min_concurrent: int = 1,
        max_concurrent: int = 10,
        scale_up_threshold: float = 0.8,
        scale_down_threshold: float = 0.3,
    ) -> None:
        if min_concurrent < 1:
            raise ValueError("min_concurrent must be >= 1")
        if max_concurrent < min_concurrent:
            raise ValueError("max_concurrent must be >= min_concurrent")
        if not (0.0 < scale_up_threshold <= 1.0):
            raise ValueError("scale_up_threshold must be in (0, 1]")
        if not (0.0 <= scale_down_threshold < scale_up_threshold):
            raise ValueError(
                "scale_down_threshold must be in [0, scale_up_threshold)"
            )

        self._min_concurrent = min_concurrent
        self._max_concurrent = max_concurrent
        self._scale_up_threshold = scale_up_threshold
        self._scale_down_threshold = scale_down_threshold

        self._current_limit = min_concurrent
        self._running_count = 0
        self._queue_depth = 0
        # Timestamp when utilization last dropped below scale_down_threshold
        self._idle_since: float | None = None

        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def current_limit(self) -> int:
        """Current concurrency limit (may change between calls)."""
        return self._current_limit

    @property
    def running_count(self) -> int:
        """Number of coroutines currently holding a slot."""
        return self._running_count

    @property
    def queue_depth(self) -> int:
        """Number of coroutines waiting to acquire a slot."""
        return self._queue_depth

    # ------------------------------------------------------------------
    # Acquire / Release
    # ------------------------------------------------------------------

    async def acquire(self) -> None:
        """Acquire a concurrency slot, waiting if necessary.

        Before granting the slot the controller checks whether the limit
        should be scaled up (high utilization) or down (sustained idle).
        """
        async with self._condition:
            self._queue_depth += 1
            try:
                while self._running_count >= self._current_limit:
                    # While waiting, try to scale up if warranted
                    self._maybe_scale_up()
                    if self._running_count < self._current_limit:
                        break
                    await self._condition.wait()
            finally:
                self._queue_depth -= 1

            self._running_count += 1
            # Evaluate scale-up right after granting the slot so the next
            # waiter has more room if load is high.
            self._maybe_scale_up()
            # Reset idle tracker since we just became busier
            self._idle_since = None

    async def release(self) -> None:
        """Release a concurrency slot and potentially scale down."""
        async with self._condition:
            if self._running_count <= 0:
                raise RuntimeError("release() called without matching acquire()")
            self._running_count -= 1
            self._maybe_scale_down()
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Context-manager support (async with controller)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DynamicConcurrencyController:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()

    # ------------------------------------------------------------------
    # Internal scaling logic (must be called under self._lock)
    # ------------------------------------------------------------------

    def _utilization(self) -> float:
        """Current utilization ratio. Returns 0.0 when limit is 0 (should not happen)."""
        if self._current_limit == 0:
            return 0.0
        return self._running_count / self._current_limit

    def _maybe_scale_up(self) -> None:
        """Increase limit by 1 if utilization >= threshold and not at ceiling."""
        if self._current_limit >= self._max_concurrent:
            return
        if self._utilization() >= self._scale_up_threshold:
            old = self._current_limit
            self._current_limit += 1
            self._idle_since = None
            logger.debug(
                "pool.scale_up",
                old_limit=old,
                new_limit=self._current_limit,
                running=self._running_count,
                queued=self._queue_depth,
            )

    def _maybe_scale_down(self) -> None:
        """Decrease limit by 1 if utilization <= threshold and idle for 5+ seconds."""
        if self._current_limit <= self._min_concurrent:
            self._idle_since = None
            return

        utilization = self._utilization()
        if utilization > self._scale_down_threshold:
            self._idle_since = None
            return

        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
            return

        if (now - self._idle_since) >= _SCALE_DOWN_IDLE_SECONDS:
            old = self._current_limit
            self._current_limit -= 1
            # Only reset if we are still below threshold after scaling down
            if self._utilization() <= self._scale_down_threshold:
                self._idle_since = now  # allow further scale-down after another 5s
            else:
                self._idle_since = None
            logger.debug(
                "pool.scale_down",
                old_limit=old,
                new_limit=self._current_limit,
                running=self._running_count,
                idle_seconds=now - (self._idle_since or now),
            )
