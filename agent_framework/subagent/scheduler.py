from __future__ import annotations

import asyncio
import time
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import SubAgentHandle, SubAgentResult

logger = get_logger(__name__)


class SubAgentScheduler:
    """Manages concurrent sub-agent execution with quota enforcement.

    API per doc section 14.4:
    - submit(handle, coro, deadline_ms) -> SubAgentHandle
    - await_result(handle) -> SubAgentResult
    - cancel(handle) -> None
    - get_quota_status(parent_run_id) -> QuotaStatus
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        max_per_run: int = 5,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_run = max_per_run
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: dict[str, SubAgentHandle] = {}  # spawn_id -> handle
        self._tasks: dict[str, asyncio.Task] = {}  # spawn_id -> task
        self._results: dict[str, SubAgentResult] = {}  # spawn_id -> result
        self._run_counts: dict[str, int] = {}  # parent_run_id -> count

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------

    def check_quota(self, parent_run_id: str) -> bool:
        """Check if a new sub-agent can be spawned under the given parent run."""
        count = self._run_counts.get(parent_run_id, 0)
        return count < self._max_per_run

    def _enforce_quota(self, parent_run_id: str) -> None:
        """Raise if quota exceeded."""
        if not self.check_quota(parent_run_id):
            raise RuntimeError(
                f"Sub-agent quota exceeded: {self._run_counts.get(parent_run_id, 0)}"
                f"/{self._max_per_run} for run {parent_run_id}"
            )

    def get_quota_status(self, parent_run_id: str) -> dict:
        """Return quota status for a parent run (section 14.4)."""
        count = self._run_counts.get(parent_run_id, 0)
        active = len([
            h for h in self._active.values()
            if h.parent_run_id == parent_run_id
        ])
        return {
            "parent_run_id": parent_run_id,
            "total_spawned": count,
            "max_per_run": self._max_per_run,
            "active_count": active,
            "max_concurrent": self._max_concurrent,
            "quota_remaining": max(0, self._max_per_run - count),
        }

    # ------------------------------------------------------------------
    # Core API (doc 14.4): submit + await_result
    # ------------------------------------------------------------------

    def submit(
        self,
        handle: SubAgentHandle,
        coro: Any,  # coroutine that returns SubAgentResult
        deadline_ms: int = 60000,
    ) -> SubAgentHandle:
        """Submit a sub-agent coroutine for execution. Returns the handle immediately.

        The actual execution runs in the background under concurrency + timeout control.
        Use await_result(handle) to wait for the result.
        """
        parent_run_id = handle.parent_run_id
        spawn_id = handle.spawn_id

        self._enforce_quota(parent_run_id)

        self._run_counts[parent_run_id] = self._run_counts.get(parent_run_id, 0) + 1
        self._active[spawn_id] = handle
        handle.status = "PENDING"

        async def _wrapped() -> SubAgentResult:
            start = time.monotonic()
            handle.status = "RUNNING"
            try:
                async with self._semaphore:
                    result = await asyncio.wait_for(
                        coro, timeout=deadline_ms / 1000.0
                    )
                duration = int((time.monotonic() - start) * 1000)
                result.duration_ms = duration
                handle.status = "COMPLETED" if result.success else "FAILED"
                logger.info(
                    "subagent.completed",
                    spawn_id=spawn_id,
                    success=result.success,
                    duration_ms=duration,
                )
                return result
            except asyncio.TimeoutError:
                handle.status = "TIMEOUT"
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error=f"Sub-agent timed out after {deadline_ms}ms",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            except asyncio.CancelledError:
                handle.status = "CANCELLED"
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error="Sub-agent was cancelled",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            except Exception as e:
                handle.status = "FAILED"
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error=str(e),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            finally:
                self._active.pop(spawn_id, None)
                self._tasks.pop(spawn_id, None)

        task = asyncio.create_task(_wrapped())
        self._tasks[spawn_id] = task

        # Store result when done
        def _on_done(t: asyncio.Task) -> None:
            try:
                self._results[spawn_id] = t.result()
            except Exception:
                self._results[spawn_id] = SubAgentResult(
                    spawn_id=spawn_id, success=False, error="Task failed unexpectedly"
                )

        task.add_done_callback(_on_done)
        return handle

    async def await_result(self, handle: SubAgentHandle) -> SubAgentResult:
        """Wait for a submitted sub-agent to complete and return its result."""
        spawn_id = handle.spawn_id
        task = self._tasks.get(spawn_id)

        if task is not None:
            await task
            return self._results.pop(spawn_id, SubAgentResult(
                spawn_id=spawn_id, success=False, error="No result captured"
            ))

        # Already finished
        if spawn_id in self._results:
            return self._results.pop(spawn_id)

        return SubAgentResult(
            spawn_id=spawn_id, success=False, error=f"No task found for {spawn_id}"
        )

    # ------------------------------------------------------------------
    # Convenience: submit + await in one call (used by SubAgentRuntime)
    # ------------------------------------------------------------------

    async def schedule(
        self,
        handle: SubAgentHandle,
        coro: Any,
        deadline_ms: int = 60000,
    ) -> SubAgentResult:
        """Submit and immediately await. Convenience for synchronous spawn pattern."""
        try:
            self.submit(handle, coro, deadline_ms)
        except RuntimeError as e:
            # If submit fails before scheduling, close the coroutine to avoid warnings.
            if hasattr(coro, "close"):
                coro.close()
            return SubAgentResult(
                spawn_id=handle.spawn_id,
                success=False,
                error=str(e),
            )
        return await self.await_result(handle)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel(self, spawn_id: str) -> bool:
        """Cancel a single sub-agent by spawn_id (section 14.4)."""
        handle = self._active.get(spawn_id)
        task = self._tasks.get(spawn_id)
        if task and not task.done():
            task.cancel()
            if handle:
                handle.status = "CANCELLED"
            return True
        return False

    def get_active_children(self, parent_run_id: str) -> list[SubAgentHandle]:
        return [
            h for h in self._active.values()
            if h.parent_run_id == parent_run_id
        ]

    async def cancel_all(self, parent_run_id: str) -> int:
        """Cancel all active sub-agents for a given parent run."""
        cancelled = 0
        for spawn_id, handle in list(self._active.items()):
            if handle.parent_run_id == parent_run_id:
                task = self._tasks.get(spawn_id)
                if task and not task.done():
                    task.cancel()
                    cancelled += 1
                handle.status = "CANCELLED"
        logger.info(
            "subagent.cancel_all",
            parent_run_id=parent_run_id,
            cancelled=cancelled,
        )
        return cancelled
