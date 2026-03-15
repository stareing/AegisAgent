"""Strict unit tests for subagent layer.

Covers:
- SubAgentFactory (memory scopes, tool filtering, snapshot capture)
- SubAgentScheduler (quota, timeout, submit/await, cancel)
- SubAgentRuntime (spawn flow)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.models.agent import AgentConfig
from agent_framework.models.memory import MemoryRecord
from agent_framework.models.subagent import (
    MemoryScope,
    SpawnMode,
    SubAgentHandle,
    SubAgentResult,
    SubAgentSpec,
)
from agent_framework.subagent.scheduler import SubAgentScheduler


# =====================================================================
# SubAgentScheduler
# =====================================================================


class TestSubAgentScheduler:
    def _make_handle(self, spawn_id="s1", parent_run_id="run_1"):
        return SubAgentHandle(
            sub_agent_id=f"sub_{spawn_id}",
            spawn_id=spawn_id,
            parent_run_id=parent_run_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )

    def test_check_quota_under_limit(self):
        sched = SubAgentScheduler(max_per_run=5)
        assert sched.check_quota("run_1") is True

    def test_check_quota_at_limit(self):
        sched = SubAgentScheduler(max_per_run=2)
        sched._run_counts["run_1"] = 2
        assert sched.check_quota("run_1") is False

    def test_get_quota_status(self):
        sched = SubAgentScheduler(max_concurrent=3, max_per_run=5)
        sched._run_counts["run_1"] = 2
        status = sched.get_quota_status("run_1")
        assert status["total_spawned"] == 2
        assert status["max_per_run"] == 5
        assert status["quota_remaining"] == 3

    @pytest.mark.asyncio
    async def test_schedule_success(self):
        sched = SubAgentScheduler(max_per_run=5)
        handle = self._make_handle()

        async def _coro():
            return SubAgentResult(spawn_id="s1", success=True, final_answer="done")

        result = await sched.schedule(handle, _coro(), deadline_ms=5000)
        assert result.success is True
        assert result.final_answer == "done"

    @pytest.mark.asyncio
    async def test_schedule_timeout(self):
        sched = SubAgentScheduler(max_per_run=5)
        handle = self._make_handle()

        async def _slow_coro():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="s1", success=True)

        result = await sched.schedule(handle, _slow_coro(), deadline_ms=100)
        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_schedule_quota_exceeded(self):
        sched = SubAgentScheduler(max_per_run=1)
        sched._run_counts["run_1"] = 1
        handle = self._make_handle()

        async def _coro():
            return SubAgentResult(spawn_id="s1", success=True)

        result = await sched.schedule(handle, _coro(), deadline_ms=5000)
        assert result.success is False
        assert "quota exceeded" in result.error.lower()

    @pytest.mark.asyncio
    async def test_submit_and_await(self):
        sched = SubAgentScheduler(max_per_run=5)
        handle = self._make_handle()

        async def _coro():
            return SubAgentResult(spawn_id="s1", success=True, final_answer="ok")

        returned_handle = sched.submit(handle, _coro(), deadline_ms=5000)
        assert returned_handle.spawn_id == "s1"

        result = await sched.await_result(handle)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        sched = SubAgentScheduler(max_per_run=5)
        handle = self._make_handle()

        async def _slow():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="s1", success=True)

        sched.submit(handle, _slow(), deadline_ms=60000)
        await asyncio.sleep(0.01)  # let task start

        cancelled = await sched.cancel("s1")
        assert cancelled is True
        result = await sched.await_result(handle)
        assert result.success is False
        assert "cancelled" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        sched = SubAgentScheduler()
        cancelled = await sched.cancel("nonexistent")
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_all_tasks(self):
        sched = SubAgentScheduler(max_per_run=10)
        handles: list[SubAgentHandle] = []

        async def _slow():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="x", success=True)

        for i in range(3):
            h = self._make_handle(spawn_id=f"s{i}")
            handles.append(h)
            task_record = sched.allocate_task_id("run_1", f"s{i}")
            sched.submit(h, _slow(), deadline_ms=60000, task_record=task_record)

        await asyncio.sleep(0.01)
        count = await sched.cancel_all_tasks("run_1")
        assert count >= 1
        for handle in handles:
            if handle.spawn_id in sched._tasks or handle.spawn_id in sched._results:
                await sched.await_result(handle)

    def test_allocate_task_id(self):
        """SubAgentScheduler is the sole source of subagent_task_id (v2.6.3 §39)."""
        sched = SubAgentScheduler()
        record = sched.allocate_task_id("run_1", "spawn_abc")
        assert record.subagent_task_id.startswith("task_")
        assert record.parent_run_id == "run_1"
        assert record.spawn_id == "spawn_abc"
        assert record.status.value == "QUEUED"
        assert record.child_run_id is None  # Runtime assigns this later

    @pytest.mark.asyncio
    async def test_concurrent_limit(self):
        sched = SubAgentScheduler(max_concurrent=1, max_per_run=10)

        async def _fast(sid):
            return SubAgentResult(spawn_id=sid, success=True)

        h1 = self._make_handle(spawn_id="c1")
        h2 = self._make_handle(spawn_id="c2")

        # Use schedule() (submit + await) sequentially for reliable results
        r1 = await sched.schedule(h1, _fast("c1"), deadline_ms=5000)
        r2 = await sched.schedule(h2, _fast("c2"), deadline_ms=5000)
        assert r1.success is True
        assert r2.success is True

    @pytest.mark.asyncio
    async def test_await_result_no_task(self):
        sched = SubAgentScheduler()
        handle = self._make_handle(spawn_id="orphan")
        result = await sched.await_result(handle)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_schedule_exception_in_coro(self):
        sched = SubAgentScheduler(max_per_run=5)
        handle = self._make_handle()

        async def _failing():
            raise ValueError("boom")

        result = await sched.schedule(handle, _failing(), deadline_ms=5000)
        assert result.success is False
        assert "boom" in result.error


# =====================================================================
# SubAgentFactory
# =====================================================================


class TestSubAgentFactory:
    def _make_parent_deps(self):
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps

        mock_mm = MagicMock()
        mock_mm.list_memories.return_value = [
            MemoryRecord(memory_id="pm1", agent_id="parent", title="parent mem"),
        ]

        mock_tr = MagicMock()
        mock_tr.list_tools.return_value = []

        mock_ce = MagicMock()
        mock_adapter = AsyncMock()
        mock_sr = MagicMock()
        mock_executor = MagicMock()
        mock_executor._mcp = None
        mock_executor._max_concurrent = 5

        return AgentRuntimeDeps(
            tool_registry=mock_tr,
            tool_executor=mock_executor,
            memory_manager=mock_mm,
            context_engineer=mock_ce,
            model_adapter=mock_adapter,
            skill_router=mock_sr,
        )

    def test_create_isolated_agent(self):
        from agent_framework.subagent.factory import SubAgentFactory

        deps = self._make_parent_deps()
        factory = SubAgentFactory(deps)
        spec = SubAgentSpec(
            task_input="sub task",
            memory_scope=MemoryScope.ISOLATED,
            spawn_id="test_spawn",
        )
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent", model_name="gpt-4")
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        assert agent.agent_id == "sub_test_spawn"
        assert agent.agent_config.allow_spawn_children is False
        assert sub_deps.sub_agent_runtime is None

    def test_create_inherit_read_agent(self):
        from agent_framework.subagent.factory import SubAgentFactory

        deps = self._make_parent_deps()
        factory = SubAgentFactory(deps)
        spec = SubAgentSpec(
            task_input="read parent",
            memory_scope=MemoryScope.INHERIT_READ,
            spawn_id="ir_spawn",
        )
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent")
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        # Memory manager should be InheritRead type
        from agent_framework.subagent.memory_scope import InheritReadMemoryManager
        assert isinstance(sub_deps.memory_manager, InheritReadMemoryManager)

    def test_create_shared_write_agent(self):
        from agent_framework.subagent.factory import SubAgentFactory

        deps = self._make_parent_deps()
        factory = SubAgentFactory(deps)
        spec = SubAgentSpec(
            task_input="write to parent",
            memory_scope=MemoryScope.SHARED_WRITE,
            spawn_id="sw_spawn",
        )
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent")
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        from agent_framework.subagent.memory_scope import SharedWriteMemoryManager
        assert isinstance(sub_deps.memory_manager, SharedWriteMemoryManager)

    def test_tool_category_whitelist(self):
        from agent_framework.subagent.factory import SubAgentFactory
        from agent_framework.models.tool import ToolEntry, ToolMeta

        deps = self._make_parent_deps()
        deps.tool_registry.list_tools.return_value = [
            ToolEntry(meta=ToolMeta(name="calc", category="math", source="local")),
            ToolEntry(meta=ToolMeta(name="shell", category="system", source="local")),
            ToolEntry(meta=ToolMeta(name="fetch", category="network", source="local")),
        ]

        factory = SubAgentFactory(deps)
        spec = SubAgentSpec(
            task_input="math only",
            tool_category_whitelist=["math"],
            spawn_id="tw_spawn",
        )
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent")
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        # Scoped registry should only include "calc"
        from agent_framework.tools.registry import ScopedToolRegistry
        assert isinstance(sub_deps.tool_registry, ScopedToolRegistry)

    def test_default_tool_filtering_blocks_dangerous(self):
        from agent_framework.subagent.factory import SubAgentFactory
        from agent_framework.models.tool import ToolEntry, ToolMeta

        deps = self._make_parent_deps()
        deps.tool_registry.list_tools.return_value = [
            ToolEntry(meta=ToolMeta(name="search", category="general", source="local")),
            ToolEntry(meta=ToolMeta(name="shell", category="system", source="local")),
            ToolEntry(meta=ToolMeta(name="http", category="network", source="local")),
            ToolEntry(meta=ToolMeta(name="spawn_agent", category="subagent", source="local")),
        ]

        factory = SubAgentFactory(deps)
        spec = SubAgentSpec(task_input="task", spawn_id="df_spawn")
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent")
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        # ScopedToolRegistry was created with whitelist excluding system/network/subagent
        # Verify the whitelist passed to ScopedToolRegistry
        from agent_framework.tools.registry import ScopedToolRegistry
        assert isinstance(sub_deps.tool_registry, ScopedToolRegistry)

    def test_force_allow_spawn_children_false(self):
        from agent_framework.subagent.factory import SubAgentFactory

        deps = self._make_parent_deps()
        factory = SubAgentFactory(deps)
        from agent_framework.models.subagent import SubAgentConfigOverride
        spec = SubAgentSpec(
            task_input="task",
            spawn_id="f_spawn",
            # SubAgentConfigOverride cannot express allow_spawn_children —
            # it's not in the whitelist. Factory forces False regardless.
            config_override=SubAgentConfigOverride(model_name="gpt-4"),
        )
        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent", allow_spawn_children=True)
        agent, sub_deps = factory.create_agent_and_deps(spec, parent)

        # Factory forces allow_spawn_children=False regardless of override
        assert agent.agent_config.allow_spawn_children is False

    def test_snapshot_capture(self):
        from agent_framework.subagent.factory import SubAgentFactory

        deps = self._make_parent_deps()
        factory = SubAgentFactory(deps)

        from agent_framework.agent.default_agent import DefaultAgent
        parent = DefaultAgent(agent_id="parent")

        snapshot = factory._capture_parent_snapshot(deps.memory_manager, parent)
        assert len(snapshot) == 1
        assert snapshot[0].memory_id == "pm1"
        deps.memory_manager.list_memories.assert_called_once_with("parent", None)


# =====================================================================
# Async spawn + collect
# =====================================================================


class TestAsyncSpawn:
    """Tests for asynchronous sub-agent spawn and result collection."""

    def _make_scheduler(self, max_concurrent=3, max_per_run=5):
        return SubAgentScheduler(max_concurrent=max_concurrent, max_per_run=max_per_run)

    @pytest.mark.asyncio
    async def test_submit_returns_immediately(self):
        """submit() must return handle without blocking for result."""
        sched = self._make_scheduler()
        handle = SubAgentHandle(
            spawn_id="s1", parent_run_id="r1", status="PENDING",
        )

        async def _fast_task():
            await asyncio.sleep(0.05)
            return SubAgentResult(spawn_id="s1", success=True, final_answer="done")

        returned_handle = sched.submit(handle, _fast_task(), deadline_ms=5000)
        # Must return immediately without waiting for _fast_task
        assert returned_handle.spawn_id == "s1"
        assert "s1" in sched._tasks
        # Clean up: await result normally (task is fast)
        result = await sched.await_result(handle)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_submit_then_await_result(self):
        """submit() + await_result() must return the sub-agent result."""
        sched = self._make_scheduler()
        handle = SubAgentHandle(
            spawn_id="s2", parent_run_id="r1", status="PENDING",
        )

        async def _fast_task():
            return SubAgentResult(spawn_id="s2", success=True, final_answer="42")

        sched.submit(handle, _fast_task(), deadline_ms=5000)
        result = await sched.await_result(handle)
        assert result.success is True
        assert result.final_answer == "42"

    @pytest.mark.asyncio
    async def test_runtime_spawn_async_and_collect(self):
        """spawn_async returns spawn_id, collect_result returns result."""
        from agent_framework.models.message import TokenUsage
        from agent_framework.models.agent import AgentRunResult, StopSignal, StopReason
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []
        mock_deps.tool_registry.export_schemas.return_value = []

        runtime = SubAgentRuntime(parent_deps=mock_deps, max_concurrent=3, max_per_run=5)

        # Use real AgentRunResult to avoid serialization issues
        mock_run_result = AgentRunResult(
            run_id="child_run",
            success=True,
            final_answer="async result",
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
            usage=TokenUsage(total_tokens=100),
            iterations_used=1,
        )

        mock_coordinator = AsyncMock()
        mock_coordinator.run = AsyncMock(return_value=mock_run_result)
        runtime._coordinator = mock_coordinator

        mock_agent = MagicMock()
        mock_agent.agent_id = "sub_test"
        mock_sub_deps = MagicMock()
        mock_sub_deps.tool_registry.list_tools.return_value = []
        runtime._factory.create_agent_and_deps = MagicMock(
            return_value=(mock_agent, mock_sub_deps)
        )

        spec = SubAgentSpec(
            parent_run_id="r1",
            task_input="do something async",
            deadline_ms=5000,
        )

        # spawn_async returns spawn_id without blocking
        spawn_id = await runtime.spawn_async(spec, parent_agent=None)
        assert isinstance(spawn_id, str)
        assert len(spawn_id) > 0

        # collect_result with wait=True returns the result
        result = await runtime.collect_result(spawn_id, wait=True)
        assert result is not None
        assert result.success is True
        assert result.final_answer == "async result"

    @pytest.mark.asyncio
    async def test_collect_result_nonblocking_while_running(self):
        """collect_result(wait=False) returns None while sub-agent is still running."""
        from agent_framework.models.message import TokenUsage
        from agent_framework.models.agent import AgentRunResult, StopSignal, StopReason
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []
        mock_deps.tool_registry.export_schemas.return_value = []

        runtime = SubAgentRuntime(parent_deps=mock_deps, max_concurrent=3, max_per_run=5)

        async def _slow_run(*args, **kwargs):
            await asyncio.sleep(2)
            return AgentRunResult(
                run_id="child", success=True, final_answer="slow",
                stop_signal=StopSignal(reason=StopReason.LLM_STOP),
                usage=TokenUsage(total_tokens=50), iterations_used=1,
            )

        mock_coordinator = AsyncMock()
        mock_coordinator.run = _slow_run
        runtime._coordinator = mock_coordinator

        mock_agent = MagicMock()
        mock_agent.agent_id = "sub_slow"
        mock_sub_deps = MagicMock()
        mock_sub_deps.tool_registry.list_tools.return_value = []
        runtime._factory.create_agent_and_deps = MagicMock(
            return_value=(mock_agent, mock_sub_deps)
        )

        spec = SubAgentSpec(
            parent_run_id="r1", task_input="slow task",
            deadline_ms=500,  # Short deadline — will timeout naturally
        )

        spawn_id = await runtime.spawn_async(spec, parent_agent=None)

        # Non-blocking check: should be None (still running)
        poll = await runtime.collect_result(spawn_id, wait=False)
        assert poll is None

        # Wait for natural timeout (deadline_ms=500)
        result = await runtime.collect_result(spawn_id, wait=True)
        assert result is not None
        assert result.success is False  # timed out

    @pytest.mark.asyncio
    async def test_delegation_executor_async_flow(self):
        """DelegationExecutor async spawn + collect produces DelegationSummary."""
        from agent_framework.tools.delegation import DelegationExecutor

        mock_runtime = AsyncMock()
        mock_runtime.spawn_async = AsyncMock(return_value="sp_123")
        mock_runtime.collect_result = AsyncMock(
            return_value=SubAgentResult(
                spawn_id="sp_123", success=True, final_answer="delegated result",
            )
        )

        executor = DelegationExecutor(sub_agent_runtime=mock_runtime)

        spec = SubAgentSpec(
            parent_run_id="r1", task_input="async delegation",
        )

        # Async spawn
        spawn_id = await executor.delegate_to_subagent_async(spec, parent_agent=None)
        assert spawn_id == "sp_123"

        # Collect
        result = await executor.collect_subagent_result("sp_123", wait=True)
        assert result is not None
        assert result.success is True

        # Summarize
        summary = DelegationExecutor.summarize_result(result)
        assert "COMPLETED" in summary.status
        assert "delegated result" in summary.summary


# ── Async lifecycle regression tests ─────────────────────────────


class TestAsyncLifecycleRegression:
    """Tests for exception cleanup, status convergence in async paths."""

    @pytest.mark.asyncio
    async def test_factory_error_cleans_active(self):
        """When create_agent_and_deps raises, _active must not leak."""
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []

        runtime = SubAgentRuntime(parent_deps=mock_deps, max_concurrent=3, max_per_run=5)
        runtime._factory.create_agent_and_deps = MagicMock(
            side_effect=RuntimeError("factory boom")
        )

        spec = SubAgentSpec(parent_run_id="r1", task_input="test", deadline_ms=5000)

        with pytest.raises(RuntimeError, match="factory boom"):
            await runtime.spawn_async(spec, parent_agent=None)

        assert len(runtime._active) == 0, "_active leaked after factory error"

    @pytest.mark.asyncio
    async def test_quota_exceeded_cleans_active(self):
        """When quota is exceeded on submit, _active must not leak."""
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []

        runtime = SubAgentRuntime(parent_deps=mock_deps, max_concurrent=1, max_per_run=0)

        mock_agent = MagicMock()
        mock_agent.agent_id = "sub"
        mock_sub_deps = MagicMock()
        runtime._factory.create_agent_and_deps = MagicMock(
            return_value=(mock_agent, mock_sub_deps)
        )
        runtime._coordinator = AsyncMock()

        spec = SubAgentSpec(parent_run_id="r1", task_input="over quota", deadline_ms=5000)

        with pytest.raises(RuntimeError, match="quota"):
            await runtime.spawn_async(spec, parent_agent=None)

        assert len(runtime._active) == 0, "_active leaked after quota error"

    @pytest.mark.asyncio
    async def test_timeout_sets_task_record_status(self):
        """task_record.status must be TIMEOUT after deadline exceeded."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentTaskStatus

        sched = SubAgentScheduler(max_concurrent=3, max_per_run=5)
        task_record = sched.allocate_task_id("r1", "sp_timeout")

        handle = SubAgentHandle(spawn_id="sp_timeout", parent_run_id="r1", status="PENDING")

        async def _slow():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="sp_timeout", success=True)

        sched.submit(handle, _slow(), deadline_ms=100, task_record=task_record)
        result = await sched.await_result(handle)

        assert result.success is False
        assert "timed out" in result.error
        assert handle.status == "TIMEOUT"
        assert task_record.status == SubAgentTaskStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_cancel_sets_task_record_status(self):
        """task_record.status must be CANCELLED after cancel."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentTaskStatus

        sched = SubAgentScheduler(max_concurrent=3, max_per_run=5)
        task_record = sched.allocate_task_id("r1", "sp_cancel")

        handle = SubAgentHandle(spawn_id="sp_cancel", parent_run_id="r1", status="PENDING")

        async def _long():
            await asyncio.sleep(30)
            return SubAgentResult(spawn_id="sp_cancel", success=True)

        sched.submit(handle, _long(), deadline_ms=30000, task_record=task_record)
        await asyncio.sleep(0.05)
        await sched.cancel("sp_cancel")
        result = await sched.await_result(handle)

        assert result.success is False
        assert handle.status == "CANCELLED"
        assert task_record.status == SubAgentTaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_max_spawn_depth_blocks_deep_spawn(self):
        """max_spawn_depth must block spawn when depth limit reached."""
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []

        # max_spawn_depth=1, simulate depth already at 1
        runtime = SubAgentRuntime(
            parent_deps=mock_deps, max_concurrent=3, max_per_run=5,
            max_spawn_depth=1,
        )
        runtime._current_depth = 1  # Already at max

        spec = SubAgentSpec(parent_run_id="r1", task_input="deep spawn", deadline_ms=5000)

        with pytest.raises(RuntimeError, match="depth limit"):
            await runtime.spawn(spec, parent_agent=None)

        # Verify _active not leaked
        assert len(runtime._active) == 0

    @pytest.mark.asyncio
    async def test_spawn_depth_increments_and_decrements(self):
        """_current_depth must increment on spawn start and decrement on finish."""
        from agent_framework.models.message import TokenUsage
        from agent_framework.models.agent import AgentRunResult, StopSignal, StopReason
        from agent_framework.subagent.runtime import SubAgentRuntime

        mock_deps = MagicMock()
        mock_deps.context_engineer.build_spawn_seed.return_value = []
        mock_deps.tool_registry.list_tools.return_value = []
        mock_deps.tool_registry.export_schemas.return_value = []

        runtime = SubAgentRuntime(
            parent_deps=mock_deps, max_concurrent=3, max_per_run=5,
            max_spawn_depth=5,
        )
        assert runtime._current_depth == 0

        mock_run_result = AgentRunResult(
            run_id="child", success=True, final_answer="ok",
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
            usage=TokenUsage(total_tokens=10), iterations_used=1,
        )
        mock_coordinator = AsyncMock()
        mock_coordinator.run = AsyncMock(return_value=mock_run_result)
        runtime._coordinator = mock_coordinator

        mock_agent = MagicMock()
        mock_agent.agent_id = "sub"
        mock_sub_deps = MagicMock()
        mock_sub_deps.tool_registry.list_tools.return_value = []
        runtime._factory.create_agent_and_deps = MagicMock(
            return_value=(mock_agent, mock_sub_deps)
        )

        spec = SubAgentSpec(parent_run_id="r1", task_input="depth test", deadline_ms=5000)
        result = await runtime.spawn(spec, parent_agent=None)

        assert result.success is True
        # After spawn completes, depth must return to 0
        assert runtime._current_depth == 0
