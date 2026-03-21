"""Tests for DynamicConcurrencyController and scheduler dynamic pool integration."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from agent_framework.subagent.pool import (
    DynamicConcurrencyController,
    _SCALE_DOWN_IDLE_SECONDS,
)


# ---------------------------------------------------------------
# Unit tests: DynamicConcurrencyController
# ---------------------------------------------------------------


class TestDynamicConcurrencyControllerInit:
    """Validation of constructor constraints."""

    def test_defaults(self):
        ctrl = DynamicConcurrencyController()
        assert ctrl.current_limit == 1
        assert ctrl.running_count == 0
        assert ctrl.queue_depth == 0

    def test_min_greater_than_max_raises(self):
        with pytest.raises(ValueError, match="max_concurrent"):
            DynamicConcurrencyController(min_concurrent=5, max_concurrent=3)

    def test_min_zero_raises(self):
        with pytest.raises(ValueError, match="min_concurrent"):
            DynamicConcurrencyController(min_concurrent=0)

    def test_invalid_scale_up_threshold(self):
        with pytest.raises(ValueError, match="scale_up_threshold"):
            DynamicConcurrencyController(scale_up_threshold=0.0)

    def test_invalid_scale_down_threshold(self):
        with pytest.raises(ValueError, match="scale_down_threshold"):
            DynamicConcurrencyController(
                scale_up_threshold=0.8, scale_down_threshold=0.9
            )


class TestDynamicConcurrencyControllerScaleUp:
    """Scale-up on high utilization."""

    @pytest.mark.asyncio
    async def test_scale_up_when_utilization_high(self):
        """When running_count / current_limit >= scale_up_threshold, limit increases."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=5,
            scale_up_threshold=0.8,
            scale_down_threshold=0.2,
        )
        assert ctrl.current_limit == 1

        # Acquire the single slot => utilization = 1/1 = 1.0 >= 0.8
        await ctrl.acquire()
        # After acquire, scale-up should have fired
        assert ctrl.current_limit == 2
        assert ctrl.running_count == 1

        # Acquire second slot => utilization = 2/2 = 1.0 >= 0.8
        await ctrl.acquire()
        assert ctrl.current_limit == 3
        assert ctrl.running_count == 2

        await ctrl.release()
        await ctrl.release()

    @pytest.mark.asyncio
    async def test_scale_up_respects_ceiling(self):
        """Limit cannot exceed max_concurrent."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=2,
            scale_up_threshold=0.8,
        )
        # Fill up to ceiling
        await ctrl.acquire()  # limit becomes 2
        assert ctrl.current_limit == 2
        await ctrl.acquire()  # at ceiling, no more scale-up
        assert ctrl.current_limit == 2
        assert ctrl.running_count == 2

        await ctrl.release()
        await ctrl.release()

    @pytest.mark.asyncio
    async def test_multiple_tasks_trigger_progressive_scale_up(self):
        """Launching many tasks scales the limit progressively."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=10,
            scale_up_threshold=0.8,
        )
        slots = []
        for _ in range(5):
            await ctrl.acquire()
            slots.append(True)

        # Limit should have scaled up to accommodate running tasks
        assert ctrl.current_limit >= 5
        assert ctrl.running_count == 5

        for _ in range(5):
            await ctrl.release()


class TestDynamicConcurrencyControllerScaleDown:
    """Scale-down on sustained idle."""

    @pytest.mark.asyncio
    async def test_scale_down_after_idle_period(self):
        """Limit decreases after sustained low utilization for 5+ seconds."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=5,
            scale_up_threshold=0.8,
            scale_down_threshold=0.3,
        )
        # Manually set limit high to test scale-down
        ctrl._current_limit = 4
        ctrl._running_count = 0

        # First release should NOT scale down (idle timer just started)
        # Simulate: acquire one, then release
        await ctrl.acquire()
        assert ctrl.running_count == 1

        # Set idle_since to 6 seconds ago to simulate idle period
        ctrl._idle_since = time.monotonic() - 6.0

        await ctrl.release()
        # After release with 0/4 utilization and 6s idle, should scale down
        assert ctrl.current_limit < 4

    @pytest.mark.asyncio
    async def test_scale_down_respects_floor(self):
        """Limit cannot go below min_concurrent."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=2,
            max_concurrent=5,
            scale_up_threshold=0.8,
            scale_down_threshold=0.3,
        )
        ctrl._current_limit = 2  # already at min
        ctrl._idle_since = time.monotonic() - 10.0

        await ctrl.acquire()
        await ctrl.release()
        # Should not go below min
        assert ctrl.current_limit >= 2

    @pytest.mark.asyncio
    async def test_no_scale_down_without_idle_period(self):
        """Scale-down does not happen if idle period is under 5 seconds."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=5,
            scale_up_threshold=0.8,
            scale_down_threshold=0.3,
        )
        ctrl._current_limit = 4

        # Acquire and release quickly — idle timer just started
        await ctrl.acquire()
        await ctrl.release()
        # First release: idle_since is just set; not enough time passed
        assert ctrl.current_limit == 4


class TestDynamicConcurrencyControllerConcurrency:
    """Concurrent acquire/release correctness."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire_blocks_at_limit(self):
        """Tasks beyond the limit should wait until a slot is released."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=1,
            max_concurrent=1,  # ceiling=1, so cannot scale up
            scale_up_threshold=0.8,
        )
        acquired = []

        async def worker(worker_id: int):
            await ctrl.acquire()
            acquired.append(worker_id)
            await asyncio.sleep(0.05)
            await ctrl.release()

        # Launch two workers; only one can run at a time
        t1 = asyncio.create_task(worker(1))
        t2 = asyncio.create_task(worker(2))
        await asyncio.gather(t1, t2)
        assert len(acquired) == 2

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """async with controller works correctly."""
        ctrl = DynamicConcurrencyController(
            min_concurrent=2, max_concurrent=5,
        )
        async with ctrl:
            assert ctrl.running_count == 1
        assert ctrl.running_count == 0

    @pytest.mark.asyncio
    async def test_release_without_acquire_raises(self):
        ctrl = DynamicConcurrencyController()
        with pytest.raises(RuntimeError, match="release.*without"):
            await ctrl.release()


# ---------------------------------------------------------------
# Integration: SubAgentScheduler with dynamic pool
# ---------------------------------------------------------------


class TestSchedulerDynamicPool:
    """SubAgentScheduler with dynamic_pool=True."""

    def _make_handle(self, spawn_id="sp_1"):
        from agent_framework.models.subagent import SubAgentHandle
        return SubAgentHandle(
            sub_agent_id="sub_1", spawn_id=spawn_id, parent_run_id="run_1",
        )

    @pytest.mark.asyncio
    async def test_scheduler_uses_dynamic_controller(self):
        """When dynamic_pool=True, scheduler uses DynamicConcurrencyController."""
        from agent_framework.subagent.scheduler import SubAgentScheduler

        sched = SubAgentScheduler(
            max_per_run=10,
            dynamic_pool=True,
            min_concurrent=1,
            max_concurrent_ceiling=5,
        )
        assert sched._concurrency_controller is not None
        assert sched._semaphore is None
        assert sched._concurrency_controller.current_limit == 1

    @pytest.mark.asyncio
    async def test_scheduler_fixed_pool_unchanged(self):
        """When dynamic_pool=False (default), scheduler uses fixed semaphore."""
        from agent_framework.subagent.scheduler import SubAgentScheduler

        sched = SubAgentScheduler(max_concurrent=3, max_per_run=10)
        assert sched._concurrency_controller is None
        assert sched._semaphore is not None

    @pytest.mark.asyncio
    async def test_dynamic_pool_schedule_success(self):
        """Tasks complete successfully through dynamic pool."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentResult

        sched = SubAgentScheduler(
            max_per_run=10,
            dynamic_pool=True,
            min_concurrent=1,
            max_concurrent_ceiling=5,
        )
        handle = self._make_handle()

        async def _work():
            return SubAgentResult(spawn_id="sp_1", success=True, final_answer="ok")

        result = await sched.schedule(handle, _work())
        assert result.success is True
        assert result.final_answer == "ok"

    @pytest.mark.asyncio
    async def test_dynamic_pool_scales_under_load(self):
        """Multiple concurrent tasks cause the controller to scale up."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentResult

        sched = SubAgentScheduler(
            max_per_run=20,
            dynamic_pool=True,
            min_concurrent=1,
            max_concurrent_ceiling=8,
        )
        controller = sched._concurrency_controller
        assert controller is not None
        assert controller.current_limit == 1

        barrier = asyncio.Event()
        results_ready = []

        async def _slow(sid: str):
            await barrier.wait()
            return SubAgentResult(spawn_id=sid, success=True)

        # Submit 5 tasks (will force scale-up)
        handles = []
        for i in range(5):
            sid = f"sp_{i}"
            h = self._make_handle(spawn_id=sid)
            sched.submit(h, _slow(sid))
            handles.append(h)

        # Give tasks a moment to enter acquire()
        await asyncio.sleep(0.1)

        # Controller should have scaled up from 1
        assert controller.current_limit > 1

        # Release all
        barrier.set()
        for h in handles:
            await sched.await_result(h)


# ---------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------


class TestSubAgentConfigDynamicPool:
    """SubAgentConfig includes dynamic pool fields."""

    def test_default_values(self):
        from agent_framework.infra.config import SubAgentConfig

        cfg = SubAgentConfig()
        assert cfg.dynamic_pool is False
        assert cfg.min_concurrent == 1
        assert cfg.max_concurrent_ceiling == 10

    def test_custom_values(self):
        from agent_framework.infra.config import SubAgentConfig

        cfg = SubAgentConfig(
            dynamic_pool=True,
            min_concurrent=2,
            max_concurrent_ceiling=20,
        )
        assert cfg.dynamic_pool is True
        assert cfg.min_concurrent == 2
        assert cfg.max_concurrent_ceiling == 20
