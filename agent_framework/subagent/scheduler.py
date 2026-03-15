from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import (
    SubAgentHandle,
    SubAgentResult,
    SubAgentTaskRecord,
    SubAgentTaskStatus,
)

logger = get_logger(__name__)


class SubAgentScheduler:
    """Manages sub-agent queuing, quota enforcement, and scheduling decisions.

    Ownership boundary (v2.6.3 §39):
    - Responsible for: queuing, quota, concurrency control, task ID assignment,
      scheduling decisions (immediate/queue/reject)
    - NOT responsible for: running sub-agents, holding execution context,
      maintaining active runtime handles, cancel execution

    The scheduler assigns subagent_task_id. The runtime assigns child_run_id.
    active_children truth source belongs to SubAgentRuntime only.

    API per doc section 14.4:
    - submit(handle, coro, deadline_ms) -> SubAgentHandle
    - await_result(handle) -> SubAgentResult
    - cancel(handle) -> None (issues cancel command; runtime executes)
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
        self._tasks: dict[str, asyncio.Task] = {}  # spawn_id -> task
        self._results: dict[str, SubAgentResult] = {}  # spawn_id -> result
        self._run_counts: dict[str, int] = {}  # parent_run_id -> count
        # Task records — scheduling-level tracking only
        self._task_records: dict[str, SubAgentTaskRecord] = {}  # task_id -> record

    # ------------------------------------------------------------------
    # Task record management
    # ------------------------------------------------------------------

    def allocate_task_id(self, parent_run_id: str, spawn_id: str) -> SubAgentTaskRecord:
        """Allocate a subagent_task_id. Sole source of task ID generation."""
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        record = SubAgentTaskRecord(
            subagent_task_id=task_id,
            parent_run_id=parent_run_id,
            spawn_id=spawn_id,
            status=SubAgentTaskStatus.QUEUED,
            scheduler_decision_ref=f"sched_{uuid.uuid4().hex[:8]}",
        )
        self._task_records[task_id] = record
        return record

    def get_task_record(self, task_id: str) -> SubAgentTaskRecord | None:
        """Get a task record by task ID."""
        return self._task_records.get(task_id)

    def _update_task_status(
        self, task_id: str, status: SubAgentTaskStatus
    ) -> None:
        """Update task record status (scheduler-owned transitions only)."""
        record = self._task_records.get(task_id)
        if record:
            record.status = status

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
            count = self._run_counts.get(parent_run_id, 0)
            logger.warning(
                "scheduler.quota_exceeded",
                parent_run_id=parent_run_id,
                current_count=count,
                max_per_run=self._max_per_run,
            )
            raise RuntimeError(
                f"Sub-agent quota exceeded: {count}"
                f"/{self._max_per_run} for run {parent_run_id}"
            )

    def get_quota_status(self, parent_run_id: str) -> dict:
        """Return quota status for a parent run (section 14.4)."""
        count = self._run_counts.get(parent_run_id, 0)
        return {
            "parent_run_id": parent_run_id,
            "total_spawned": count,
            "max_per_run": self._max_per_run,
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
        task_record: SubAgentTaskRecord | None = None,
    ) -> SubAgentHandle:
        """Submit a sub-agent coroutine for execution. Returns the handle immediately.

        The actual execution runs in the background under concurrency + timeout control.
        Use await_result(handle) to wait for the result.
        """
        parent_run_id = handle.parent_run_id
        spawn_id = handle.spawn_id

        self._enforce_quota(parent_run_id)

        self._run_counts[parent_run_id] = self._run_counts.get(parent_run_id, 0) + 1
        handle.status = "PENDING"

        # Mark task record as scheduled
        if task_record:
            self._update_task_status(
                task_record.subagent_task_id, SubAgentTaskStatus.SCHEDULED
            )

        async def _wrapped() -> SubAgentResult:
            start = time.monotonic()
            handle.status = "RUNNING"
            logger.info(
                "scheduler.task_running",
                spawn_id=spawn_id,
                deadline_ms=deadline_ms,
                parent_run_id=parent_run_id,
            )
            # Wrap coro as a managed task for clean cancellation.
            # Using ensure_future + wait (not wait_for) avoids Python's
            # "coroutine was never awaited" warning on cancel/timeout.
            inner = asyncio.ensure_future(coro)
            try:
                async with self._semaphore:
                    timeout = deadline_ms / 1000.0 if deadline_ms > 0 else None
                    done, _ = await asyncio.wait(
                        {inner}, timeout=timeout
                    )
                    if inner in done:
                        if inner.cancelled():
                            raise asyncio.CancelledError()
                        if inner.exception():
                            raise inner.exception()
                        result = inner.result()
                    else:
                        raise asyncio.TimeoutError()
                duration = int((time.monotonic() - start) * 1000)
                result.duration_ms = duration
                handle.status = "COMPLETED" if result.success else "FAILED"
                logger.info(
                    "scheduler.task_completed",
                    spawn_id=spawn_id,
                    success=result.success,
                    duration_ms=duration,
                    status=handle.status,
                )
                return result
            except asyncio.TimeoutError:
                inner.cancel()
                try:
                    await inner
                except (asyncio.CancelledError, Exception):
                    pass
                duration = int((time.monotonic() - start) * 1000)
                handle.status = "TIMEOUT"
                if task_record:
                    task_record.status = SubAgentTaskStatus.TIMEOUT
                logger.error(
                    "scheduler.task_timeout",
                    spawn_id=spawn_id,
                    deadline_ms=deadline_ms,
                    actual_duration_ms=duration,
                    parent_run_id=parent_run_id,
                )
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error=f"Sub-agent timed out after {deadline_ms}ms",
                    duration_ms=duration,
                )
            except asyncio.CancelledError:
                inner.cancel()
                try:
                    await inner
                except (asyncio.CancelledError, Exception):
                    pass
                duration = int((time.monotonic() - start) * 1000)
                handle.status = "CANCELLED"
                if task_record:
                    task_record.status = SubAgentTaskStatus.CANCELLED
                logger.warning(
                    "scheduler.task_cancelled",
                    spawn_id=spawn_id,
                    duration_ms=duration,
                    parent_run_id=parent_run_id,
                )
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error="Sub-agent was cancelled",
                    duration_ms=duration,
                )
            except Exception as e:
                duration = int((time.monotonic() - start) * 1000)
                handle.status = "FAILED"
                if task_record:
                    task_record.status = SubAgentTaskStatus.FAILED
                logger.error(
                    "scheduler.task_failed",
                    spawn_id=spawn_id,
                    error_type=type(e).__name__,
                    error=str(e),
                    duration_ms=duration,
                    parent_run_id=parent_run_id,
                )
                return SubAgentResult(
                    spawn_id=spawn_id,
                    success=False,
                    error=str(e),
                    duration_ms=duration,
                )
            finally:
                self._tasks.pop(spawn_id, None)
                # Ensure inner task is fully cleaned up
                if not inner.done():
                    inner.cancel()
                    try:
                        await inner
                    except (asyncio.CancelledError, Exception):
                        pass

        task = asyncio.create_task(_wrapped())
        self._tasks[spawn_id] = task

        # Store result when done
        def _on_done(t: asyncio.Task) -> None:
            try:
                self._results[spawn_id] = t.result()
            except Exception as e:
                logger.error(
                    "scheduler.on_done_failed",
                    spawn_id=spawn_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                self._results[spawn_id] = SubAgentResult(
                    spawn_id=spawn_id, success=False, error=f"Task failed: {e}"
                )

        task.add_done_callback(_on_done)
        return handle

    def get_result_if_ready(self, spawn_id: str) -> SubAgentResult | None:
        """Non-blocking: return result if done, else None."""
        return self._results.pop(spawn_id, None)

    def is_running(self, spawn_id: str) -> bool:
        """Check if a task is still running."""
        task = self._tasks.get(spawn_id)
        return task is not None and not task.done()

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
        task_record: SubAgentTaskRecord | None = None,
    ) -> SubAgentResult:
        """Submit and immediately await. Convenience for synchronous spawn pattern."""
        try:
            self.submit(handle, coro, deadline_ms, task_record=task_record)
        except RuntimeError as e:
            # If submit fails before scheduling, close the coroutine to avoid warnings.
            if hasattr(coro, "close"):
                coro.close()
            # Mark task as rejected
            if task_record:
                self._update_task_status(
                    task_record.subagent_task_id, SubAgentTaskStatus.REJECTED
                )
            return SubAgentResult(
                spawn_id=handle.spawn_id,
                success=False,
                error=str(e),
            )
        return await self.await_result(handle)

    # ------------------------------------------------------------------
    # Cancel — issues command only; runtime executes actual cancellation
    # ------------------------------------------------------------------

    async def cancel(self, spawn_id: str) -> bool:
        """Issue cancel command for a sub-agent by spawn_id.

        The scheduler only issues the cancel. Actual cancellation and
        final status update is performed by SubAgentRuntime (v2.6.3 §39).
        """
        task = self._tasks.get(spawn_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def cancel_all_tasks(self, parent_run_id: str) -> int:
        """Issue cancel commands for all tasks under a parent run.

        Returns the number of cancel commands issued.
        Actual cancellation is performed by SubAgentRuntime.
        """
        cancelled = 0
        for spawn_id, task in list(self._tasks.items()):
            # Find task records matching this parent
            record = None
            for tr in self._task_records.values():
                if tr.spawn_id == spawn_id and tr.parent_run_id == parent_run_id:
                    record = tr
                    break
            if record and task and not task.done():
                task.cancel()
                cancelled += 1
        logger.info(
            "scheduler.cancel_all_issued",
            parent_run_id=parent_run_id,
            cancel_commands_issued=cancelled,
        )
        return cancelled
