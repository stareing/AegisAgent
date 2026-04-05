"""LeadCollector — three-mode result collection for multi-agent orchestration.

Implements the three collection strategies for parent (Lead) agent:

Mode A (SEQUENTIAL): Collect one completed result per pull. Lead gets
    a decision window after each. Good for dependent tasks.

Mode B (BATCH_ALL): Block until all spawned agents complete, then
    return everything at once. Good for independent tasks.

Mode C (HYBRID): Each pull returns ALL currently-completed results
    (1..N). Degrades to A when only 1 completes, degrades to B when
    all complete simultaneously. Recommended default.

Architecture:
    ToolExecutor._subagent_spawn() → registers spawn_id in LeadCollector
    ToolExecutor._subagent_collect() → delegates to LeadCollector.pull()
    LeadCollector.pull() → returns results based on strategy
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Exponential backoff schedule (ms) for empty poll cycles.
# Index = min(consecutive_empty_polls, len-1).
_BACKOFF_SCHEDULE_MS: list[int] = [500, 1000, 2000, 5000, 10000]

# Sentinel value returned by collect_fn to indicate "still running".
# Using a unique object avoids collision with any real result dict.
_STILL_RUNNING = object()


def is_still_running(result: Any) -> bool:
    """Check if a collect_fn return value means 'agent still running'.

    Recognizes:
    - None (delegation.collect_subagent_result returns None when not done)
    - _STILL_RUNNING sentinel
    - Dict with _still_running=True marker (set by ToolExecutor._collect_fn)
    """
    if result is None or result is _STILL_RUNNING:
        return True
    if isinstance(result, dict) and result.get("_still_running"):
        return True
    return False


class CollectionStrategy(str, Enum):
    """How the Lead agent collects results from spawned sub-agents.

    SEQUENTIAL: Pull one completed result at a time (Mode A).
    BATCH_ALL: Wait for all spawns to complete, pull all at once (Mode B).
    HYBRID: Pull all currently-completed results each time (Mode C, default).
    """

    SEQUENTIAL = "SEQUENTIAL"
    BATCH_ALL = "BATCH_ALL"
    HYBRID = "HYBRID"


class SpawnTracker:
    """Tracks a single spawned sub-agent."""

    __slots__ = ("spawn_id", "task_input", "label", "collected")

    def __init__(self, spawn_id: str, task_input: str, label: str = "") -> None:
        self.spawn_id = spawn_id
        self.task_input = task_input
        self.label = label or spawn_id
        self.collected = False


class BatchResult(BaseModel):
    """Result of a batch pull — one or more collected results."""

    results: list[dict] = Field(default_factory=list)
    total_spawned: int = 0
    total_collected: int = 0
    still_running: int = 0
    batch_index: int = 0
    is_final_batch: bool = False


class CollectionTimeoutError(Exception):
    """Raised when collection polling exceeds max_poll_cycles."""
    pass


class LeadCollector:
    """Manages result collection for a group of spawned sub-agents.

    One LeadCollector per orchestration session (run). Tracks all spawns
    initiated by the Lead agent and collects results per the chosen strategy.
    """

    # Safety limit: max poll iterations before giving up (prevents infinite loop)
    MAX_POLL_CYCLES: int = 600  # 600 × 500ms = 5 minutes default

    def __init__(
        self,
        strategy: CollectionStrategy = CollectionStrategy.HYBRID,
        poll_interval_ms: int = 500,
        max_poll_cycles: int = 0,
    ) -> None:
        self._strategy = strategy
        self._poll_interval_s = poll_interval_ms / 1000.0
        self._max_poll_cycles = max_poll_cycles or self.MAX_POLL_CYCLES
        self._spawns: dict[str, SpawnTracker] = {}  # spawn_id → tracker
        self._batch_index: int = 0
        self._collected_total: int = 0

    @property
    def strategy(self) -> CollectionStrategy:
        return self._strategy

    @property
    def total_spawned(self) -> int:
        return len(self._spawns)

    @property
    def total_collected(self) -> int:
        return self._collected_total

    @property
    def still_running(self) -> int:
        return sum(1 for s in self._spawns.values() if not s.collected)

    @property
    def all_collected(self) -> bool:
        return self.still_running == 0 and self.total_spawned > 0

    def register_spawn(self, spawn_id: str, task_input: str, label: str = "") -> None:
        """Register a newly spawned sub-agent for tracking."""
        self._spawns[spawn_id] = SpawnTracker(spawn_id, task_input, label)
        logger.info(
            "lead_collector.spawn_registered",
            spawn_id=spawn_id,
            label=label,
            strategy=self._strategy.value,
            total_spawned=self.total_spawned,
        )

    async def pull(
        self,
        collect_fn: Any,  # async (spawn_id, wait=bool) -> dict | None
    ) -> BatchResult:
        """Pull results based on the collection strategy.

        Args:
            collect_fn: Async callable(spawn_id, wait=bool) that returns
                a result dict if complete, None if still running.

        Returns:
            BatchResult with collected results and progress counters.
        """
        if self._strategy == CollectionStrategy.SEQUENTIAL:
            return await self._pull_sequential(collect_fn)
        elif self._strategy == CollectionStrategy.BATCH_ALL:
            return await self._pull_batch_all(collect_fn)
        else:
            return await self._pull_hybrid(collect_fn)

    async def _pull_sequential(self, collect_fn: Any) -> BatchResult:
        """Mode A: Wait for exactly one result, return it."""
        pending = [s for s in self._spawns.values() if not s.collected]
        if not pending:
            return self._make_batch([])

        cycles = 0
        consecutive_empty_polls = 0
        while cycles < self._max_poll_cycles:
            for tracker in pending:
                result = await collect_fn(tracker.spawn_id, wait=False)
                if not is_still_running(result):
                    tracker.collected = True
                    self._collected_total += 1
                    result_with_label = dict(result)
                    result_with_label["_spawn_label"] = tracker.label
                    result_with_label["_spawn_id"] = tracker.spawn_id
                    return self._make_batch([result_with_label])

            backoff_idx = min(consecutive_empty_polls, len(_BACKOFF_SCHEDULE_MS) - 1)
            delay_s = _BACKOFF_SCHEDULE_MS[backoff_idx] / 1000.0
            consecutive_empty_polls += 1
            cycles += 1
            await asyncio.sleep(delay_s)

        raise CollectionTimeoutError(
            f"SEQUENTIAL poll exceeded {self._max_poll_cycles} cycles. "
            f"Pending: {[t.spawn_id for t in pending]}"
        )

    async def _pull_batch_all(self, collect_fn: Any) -> BatchResult:
        """Mode B: Wait for ALL pending to complete in parallel, return all at once.

        Uses asyncio.gather to wait for all agents concurrently, not sequentially.
        This ensures that if sp2 finishes before sp1, we don't wait for sp1 first.
        """
        pending = [s for s in self._spawns.values() if not s.collected]
        if not pending:
            return self._make_batch([])

        # Wait for all concurrently
        raw_results = await asyncio.gather(
            *(collect_fn(tracker.spawn_id, wait=True) for tracker in pending),
            return_exceptions=True,
        )

        results: list[dict] = []
        for tracker, result in zip(pending, raw_results):
            if isinstance(result, Exception):
                result = {"status": "FAILED", "error": str(result)}
            # Filter: must not be None and must not carry _still_running marker
            if result is not None and not is_still_running(result):
                tracker.collected = True
                self._collected_total += 1
                result_with_label = dict(result)
                result_with_label["_spawn_label"] = tracker.label
                result_with_label["_spawn_id"] = tracker.spawn_id
                results.append(result_with_label)

        return self._make_batch(results)

    async def _pull_hybrid(self, collect_fn: Any) -> BatchResult:
        """Mode C: Return all currently-completed results (≥1).

        Polls until at least one result is available, then returns
        ALL completed results in that poll cycle. Uses exponential
        backoff on consecutive empty polls to reduce busy-waiting.
        """
        pending = [s for s in self._spawns.values() if not s.collected]
        if not pending:
            return self._make_batch([])

        cycles = 0
        consecutive_empty_polls = 0
        while cycles < self._max_poll_cycles:
            completed_this_cycle: list[dict] = []
            for tracker in pending:
                result = await collect_fn(tracker.spawn_id, wait=False)
                if not is_still_running(result):
                    tracker.collected = True
                    self._collected_total += 1
                    result_with_label = dict(result)
                    result_with_label["_spawn_label"] = tracker.label
                    result_with_label["_spawn_id"] = tracker.spawn_id
                    completed_this_cycle.append(result_with_label)

            if completed_this_cycle:
                return self._make_batch(completed_this_cycle)

            consecutive_empty_polls += 1
            backoff_idx = min(consecutive_empty_polls, len(_BACKOFF_SCHEDULE_MS) - 1)
            delay_s = _BACKOFF_SCHEDULE_MS[backoff_idx] / 1000.0
            cycles += 1
            await asyncio.sleep(delay_s)

        raise CollectionTimeoutError(
            f"HYBRID poll exceeded {self._max_poll_cycles} cycles. "
            f"Pending: {[t.spawn_id for t in pending]}"
        )

    def _make_batch(self, results: list[dict]) -> BatchResult:
        """Build a BatchResult with current counters."""
        self._batch_index += 1
        still = self.still_running
        return BatchResult(
            results=results,
            total_spawned=self.total_spawned,
            total_collected=self.total_collected,
            still_running=still,
            batch_index=self._batch_index,
            is_final_batch=(still == 0),
        )

    def get_progress_summary(self) -> str:
        """Human-readable progress string for context injection."""
        return (
            f"{self.total_collected}/{self.total_spawned} completed, "
            f"{self.still_running} running"
        )

    def reset(self) -> None:
        """Reset for a new orchestration session."""
        self._spawns.clear()
        self._batch_index = 0
        self._collected_total = 0
