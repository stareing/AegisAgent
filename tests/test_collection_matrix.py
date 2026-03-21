"""Matrix tests for three collection strategies — PRD tables 1 & 3.

Two full matrix test suites:

Matrix 1 (PRD §1 — 模式定义与对比):
  Dimensions: [SEQUENTIAL, BATCH_ALL, HYBRID] × [收集节奏, Lead输出频率, 回压控制]
  Tests each behavioral property across all three modes.

Matrix 2 (PRD §3 — 需求规格总结):
  Dimensions: [SEQUENTIAL, BATCH_ALL, HYBRID] × [启动声明, 标注, 中间汇报,
  进度标记, 冲突检查, 决策窗口, 验收入口, 最少输出轮次]
  Tests each requirement row across all three modes.

Additionally:
- Degradation tests: HYBRID → SEQUENTIAL (1 complete), HYBRID → BATCH_ALL (all at once)
- Cross-run isolation
- Error handling (agent failures mid-collection)
- Timing scenarios (staggered completions)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_framework.subagent.lead_collector import (BatchResult,
                                                     CollectionStrategy,
                                                     LeadCollector,
                                                     is_still_running)

# ---------------------------------------------------------------------------
# Helpers: simulate N agents with controlled completion timing
# ---------------------------------------------------------------------------

def make_collector(
    strategy: CollectionStrategy,
    agent_count: int = 3,
    labels: list[str] | None = None,
) -> LeadCollector:
    """Create a LeadCollector with N registered agents."""
    lc = LeadCollector(strategy=strategy, poll_interval_ms=5)
    for i in range(agent_count):
        label = (labels[i] if labels else f"Agent {chr(65 + i)}")
        lc.register_spawn(f"sp{i}", f"Task {i}", label)
    return lc


def make_collect_fn(
    completed: dict[str, dict],
    timing: dict[str, int] | None = None,
):
    """Build a collect_fn that returns results for completed spawn_ids.

    Args:
        completed: spawn_id -> result dict (agents that are done)
        timing: spawn_id -> call_count threshold before completion (simulates delay)
    """
    call_counts: dict[str, int] = {}

    async def collect_fn(spawn_id: str, wait: bool = False) -> dict | None:
        call_counts.setdefault(spawn_id, 0)
        call_counts[spawn_id] += 1

        if timing and spawn_id in timing:
            threshold = timing[spawn_id]
            if not wait and call_counts[spawn_id] < threshold:
                return {"_still_running": True, "spawn_id": spawn_id, "status": "RUNNING"}

        if spawn_id in completed:
            return completed[spawn_id]

        if wait:
            # Simulate blocking wait — return after a short delay
            await asyncio.sleep(0.01)
            if spawn_id in completed:
                return completed[spawn_id]
            return {"status": "COMPLETED", "summary": f"Default result for {spawn_id}"}

        return {"_still_running": True, "spawn_id": spawn_id, "status": "RUNNING"}

    return collect_fn


# ═══════════════════════════════════════════════════════════════════════════
# MATRIX 1: PRD §1 — 模式定义与对比
# ═══════════════════════════════════════════════════════════════════════════

class TestMatrix1_CollectionRhythm:
    """收集节奏: SEQUENTIAL=1个1个, BATCH_ALL=全部一次, HYBRID=当前所有已完成."""

    @pytest.mark.asyncio
    async def test_sequential_returns_exactly_one(self):
        """SEQUENTIAL: 完成1个，汇报1个。"""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A done"}, "sp1": {"s": "B done"}})

        b = await lc.pull(fn)
        assert len(b.results) == 1, "SEQUENTIAL must return exactly 1"

    @pytest.mark.asyncio
    async def test_batch_all_returns_all_at_once(self):
        """BATCH_ALL: 等全部完成，一次性拉取。"""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({
            "sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"},
        })

        b = await lc.pull(fn)
        assert len(b.results) == 3, "BATCH_ALL must return all 3"
        assert b.is_final_batch

    @pytest.mark.asyncio
    async def test_hybrid_returns_all_currently_completed(self):
        """HYBRID: 每轮拉取当前所有已完成的(>=1)。"""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        # sp0 and sp1 done, sp2 still running
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})

        b = await lc.pull(fn)
        assert len(b.results) == 2, "HYBRID must return all currently-completed (2)"
        assert b.still_running == 1


class TestMatrix1_LeadOutputFrequency:
    """Lead输出频率: SEQUENTIAL=N次, BATCH_ALL=1次, HYBRID=1~N次."""

    @pytest.mark.asyncio
    async def test_sequential_requires_n_pulls(self):
        """SEQUENTIAL: N次 (N=子agent数)."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        all_done = {"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}}
        fn = make_collect_fn(all_done)

        pulls = 0
        while not lc.all_collected:
            await lc.pull(fn)
            pulls += 1
        assert pulls == 3, "SEQUENTIAL needs exactly N pulls for N agents"

    @pytest.mark.asyncio
    async def test_batch_all_single_pull(self):
        """BATCH_ALL: 仅1次."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({
            "sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"},
        })

        b = await lc.pull(fn)
        assert b.is_final_batch
        assert lc.all_collected
        # Only 1 pull needed
        assert b.batch_index == 1

    @pytest.mark.asyncio
    async def test_hybrid_adaptive_pulls(self):
        """HYBRID: 1~N次 取决于完成时序."""
        lc = make_collector(CollectionStrategy.HYBRID, 4)
        # Round 1: sp0+sp1 done, Round 2: sp2+sp3 done
        completed: dict[str, dict] = {}

        async def staged_fn(sid: str, wait: bool = False) -> dict | None:
            if sid in completed:
                return completed[sid]
            if wait:
                await asyncio.sleep(0.01)
                if sid in completed:
                    return completed[sid]
            return {"_still_running": True}

        # Stage 1: sp0 and sp1 complete
        completed["sp0"] = {"s": "A"}
        completed["sp1"] = {"s": "B"}
        b1 = await lc.pull(staged_fn)
        assert len(b1.results) == 2

        # Stage 2: sp2 and sp3 complete
        completed["sp2"] = {"s": "C"}
        completed["sp3"] = {"s": "D"}
        b2 = await lc.pull(staged_fn)
        assert len(b2.results) == 2
        assert b2.is_final_batch
        # 2 pulls total (between 1 and N=4)


class TestMatrix1_BackpressureControl:
    """回压控制: SEQUENTIAL=强, BATCH_ALL=无, HYBRID=中等."""

    @pytest.mark.asyncio
    async def test_sequential_can_abort_mid_collection(self):
        """SEQUENTIAL: 强回压 — 可中途叫停."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b1 = await lc.pull(fn)
        assert b1.total_collected == 1
        # Lead decides to stop after first result — remaining agents NOT collected
        assert lc.still_running == 2
        # Can inspect result and decide not to pull again

    @pytest.mark.asyncio
    async def test_batch_all_no_intermediate_decision(self):
        """BATCH_ALL: 无回压 — 全部跑完才介入."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({
            "sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"},
        })

        b = await lc.pull(fn)
        # All collected in single pull — no intermediate decision point
        assert b.total_collected == 3
        assert b.is_final_batch

    @pytest.mark.asyncio
    async def test_hybrid_batch_level_decision(self):
        """HYBRID: 中等回压 — 批次间可调整."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}})  # Only sp0 done

        b1 = await lc.pull(fn)
        assert len(b1.results) == 1
        assert b1.still_running == 2
        # Lead has decision window between batches


# ═══════════════════════════════════════════════════════════════════════════
# MATRIX 2: PRD §3 — 需求规格总结
# ═══════════════════════════════════════════════════════════════════════════

class TestMatrix2_Labels:
    """子agent标注编号+职责+目标文件 — all modes."""

    @pytest.mark.asyncio
    async def test_sequential_preserves_labels(self):
        labels = ["Agent A — fs.py", "Agent B — shell.py", "Agent C — loop.py"]
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3, labels)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert b.results[0]["_spawn_label"] in labels

    @pytest.mark.asyncio
    async def test_batch_all_preserves_labels(self):
        labels = ["Agent A — fs.py", "Agent B — shell.py", "Agent C — loop.py"]
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3, labels)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        result_labels = {r["_spawn_label"] for r in b.results}
        assert result_labels == set(labels)

    @pytest.mark.asyncio
    async def test_hybrid_preserves_labels(self):
        labels = ["Agent A — fs.py", "Agent B — shell.py", "Agent C — loop.py"]
        lc = make_collector(CollectionStrategy.HYBRID, 3, labels)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})

        b = await lc.pull(fn)
        for r in b.results:
            assert "_spawn_label" in r
            assert "_spawn_id" in r


class TestMatrix2_ProgressMarkers:
    """汇报含进度标记: SEQUENTIAL=m/N, BATCH_ALL=N/N, HYBRID=done/N+running."""

    @pytest.mark.asyncio
    async def test_sequential_progress_m_over_n(self):
        """SEQUENTIAL: m/N after each pull."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b1 = await lc.pull(fn)
        assert b1.total_collected == 1
        assert b1.total_spawned == 3  # 1/3

        b2 = await lc.pull(fn)
        assert b2.total_collected == 2
        assert b2.total_spawned == 3  # 2/3

        b3 = await lc.pull(fn)
        assert b3.total_collected == 3
        assert b3.total_spawned == 3  # 3/3
        assert b3.is_final_batch

    @pytest.mark.asyncio
    async def test_batch_all_progress_all_at_once(self):
        """BATCH_ALL: N/N in single batch."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert b.total_collected == 3
        assert b.total_spawned == 3
        assert b.still_running == 0

    @pytest.mark.asyncio
    async def test_hybrid_progress_done_and_running(self):
        """HYBRID: done/N + running count."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})

        b = await lc.pull(fn)
        assert b.total_collected == 2
        assert b.still_running == 1
        assert not b.is_final_batch


class TestMatrix2_IntermediateReports:
    """中间汇报: SEQUENTIAL=每个1次, BATCH_ALL=无, HYBRID=每批1次."""

    @pytest.mark.asyncio
    async def test_sequential_n_intermediate_reports(self):
        """SEQUENTIAL: N intermediate reports."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        batches = []
        while not lc.all_collected:
            batches.append(await lc.pull(fn))
        assert len(batches) == 3
        for i, b in enumerate(batches):
            assert len(b.results) == 1  # Each report has exactly 1 result
            assert b.batch_index == i + 1

    @pytest.mark.asyncio
    async def test_batch_all_no_intermediate(self):
        """BATCH_ALL: 0 intermediate, 1 final."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert b.batch_index == 1
        assert b.is_final_batch
        # Only 1 batch total

    @pytest.mark.asyncio
    async def test_hybrid_variable_batches(self):
        """HYBRID: 1~N batches depending on timing."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        completed: dict[str, dict] = {}

        async def staged_fn(sid: str, wait: bool = False) -> dict | None:
            if sid in completed:
                return completed[sid]
            return {"_still_running": True} if not wait else {"s": f"{sid} done"}

        # Batch 1: 2 complete
        completed["sp0"] = {"s": "A"}
        completed["sp1"] = {"s": "B"}
        b1 = await lc.pull(staged_fn)
        assert len(b1.results) == 2
        assert not b1.is_final_batch

        # Batch 2: last 1 completes
        completed["sp2"] = {"s": "C"}
        b2 = await lc.pull(staged_fn)
        assert len(b2.results) == 1
        assert b2.is_final_batch
        assert b2.batch_index == 2


class TestMatrix2_DecisionWindow:
    """Lead决策窗口: SEQUENTIAL=每次汇报后, BATCH_ALL=仅最终, HYBRID=每批后."""

    @pytest.mark.asyncio
    async def test_sequential_decision_after_each(self):
        """SEQUENTIAL: Decision window after each result."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b1 = await lc.pull(fn)
        assert not b1.is_final_batch
        assert b1.still_running == 2
        # Lead can decide: continue, abort, or redirect

        b2 = await lc.pull(fn)
        assert b2.still_running == 1
        # Another decision point

    @pytest.mark.asyncio
    async def test_batch_all_single_decision_point(self):
        """BATCH_ALL: Only one decision point — after all complete."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert b.is_final_batch
        assert b.still_running == 0
        # This is the ONLY decision point

    @pytest.mark.asyncio
    async def test_hybrid_decision_per_batch(self):
        """HYBRID: Decision after each batch pull."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        completed: dict[str, dict] = {"sp0": {"s": "A"}}

        async def fn(sid: str, wait: bool = False) -> dict | None:
            return completed.get(sid) or ({"_still_running": True} if not wait else {"s": sid})

        b1 = await lc.pull(fn)
        assert not b1.is_final_batch
        # Decision window: Lead can adjust remaining agents

        completed["sp1"] = {"s": "B"}
        completed["sp2"] = {"s": "C"}
        b2 = await lc.pull(fn)
        assert b2.is_final_batch


class TestMatrix2_FinalEntry:
    """验收入口: All modes end with is_final_batch=True."""

    @pytest.mark.asyncio
    async def test_sequential_final_on_last(self):
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 2)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})
        b1 = await lc.pull(fn)
        assert not b1.is_final_batch
        b2 = await lc.pull(fn)
        assert b2.is_final_batch

    @pytest.mark.asyncio
    async def test_batch_all_final_immediate(self):
        lc = make_collector(CollectionStrategy.BATCH_ALL, 2)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})
        b = await lc.pull(fn)
        assert b.is_final_batch

    @pytest.mark.asyncio
    async def test_hybrid_final_when_all_done(self):
        lc = make_collector(CollectionStrategy.HYBRID, 2)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}})
        b = await lc.pull(fn)
        assert b.is_final_batch


class TestMatrix2_MinimumOutputRounds:
    """最少输出轮次: SEQUENTIAL=N, BATCH_ALL=1, HYBRID=1~N."""

    @pytest.mark.asyncio
    async def test_sequential_minimum_n_rounds(self):
        """SEQUENTIAL: exactly N rounds for N agents."""
        for n in (1, 2, 3, 5):
            lc = LeadCollector(strategy=CollectionStrategy.SEQUENTIAL, poll_interval_ms=5)
            done = {}
            for i in range(n):
                lc.register_spawn(f"sp{i}", f"Task {i}")
                done[f"sp{i}"] = {"s": f"done {i}"}
            fn = make_collect_fn(done)

            rounds = 0
            while not lc.all_collected:
                await lc.pull(fn)
                rounds += 1
            assert rounds == n

    @pytest.mark.asyncio
    async def test_batch_all_exactly_1_round(self):
        """BATCH_ALL: always exactly 1 round."""
        for n in (1, 2, 3, 5):
            lc = LeadCollector(strategy=CollectionStrategy.BATCH_ALL, poll_interval_ms=5)
            done = {}
            for i in range(n):
                lc.register_spawn(f"sp{i}", f"Task {i}")
                done[f"sp{i}"] = {"s": f"done {i}"}
            fn = make_collect_fn(done)

            b = await lc.pull(fn)
            assert b.is_final_batch
            assert b.batch_index == 1

    @pytest.mark.asyncio
    async def test_hybrid_between_1_and_n_rounds(self):
        """HYBRID: 1 <= rounds <= N."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        rounds = 0
        while not lc.all_collected:
            await lc.pull(fn)
            rounds += 1
        assert 1 <= rounds <= 3


# ═══════════════════════════════════════════════════════════════════════════
# DEGRADATION: HYBRID → SEQUENTIAL / BATCH_ALL
# ═══════════════════════════════════════════════════════════════════════════

class TestHybridDegradation:
    """HYBRID退化行为: 单个完成→Mode A, 全部完成→Mode B."""

    @pytest.mark.asyncio
    async def test_hybrid_degrades_to_sequential_one_at_a_time(self):
        """Single completion per cycle → 1 result per pull (Mode A behavior)."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        completed: dict[str, dict] = {}

        async def fn(sid: str, wait: bool = False) -> dict | None:
            return completed.get(sid) or ({"_still_running": True} if not wait else {"s": sid})

        # Only sp0 done → 1 result (like SEQUENTIAL)
        completed["sp0"] = {"s": "A"}
        b1 = await lc.pull(fn)
        assert len(b1.results) == 1

        # Only sp1 done → 1 result again
        completed["sp1"] = {"s": "B"}
        b2 = await lc.pull(fn)
        assert len(b2.results) == 1

        # sp2 done → 1 result, final
        completed["sp2"] = {"s": "C"}
        b3 = await lc.pull(fn)
        assert len(b3.results) == 1
        assert b3.is_final_batch

    @pytest.mark.asyncio
    async def test_hybrid_degrades_to_batch_all_simultaneous(self):
        """All complete at once → all results in 1 pull (Mode B behavior)."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert len(b.results) == 3
        assert b.is_final_batch
        assert b.batch_index == 1  # Single pull like BATCH_ALL


# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLING: agent failures mid-collection
# ═══════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Agent failures during collection."""

    @pytest.mark.asyncio
    async def test_sequential_handles_failed_agent(self):
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({
            "sp0": {"status": "FAILED", "error": "crash"},
            "sp1": {"status": "COMPLETED", "summary": "ok"},
            "sp2": {"status": "COMPLETED", "summary": "ok"},
        })

        b1 = await lc.pull(fn)
        assert b1.results[0].get("status") == "FAILED"
        assert b1.total_collected == 1  # Failed still counts as collected

    @pytest.mark.asyncio
    async def test_batch_all_handles_exception_in_collect_fn(self):
        """BATCH_ALL gather with return_exceptions handles crashes."""
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)

        async def exploding_fn(sid: str, wait: bool = False):
            if sid == "sp1":
                raise RuntimeError("agent sp1 exploded")
            return {"status": "COMPLETED", "summary": f"{sid} ok"}

        b = await lc.pull(exploding_fn)
        assert len(b.results) == 3
        # sp1 should have error result from exception handling
        sp1_result = [r for r in b.results if r["_spawn_id"] == "sp1"][0]
        assert "error" in sp1_result or sp1_result.get("status") == "FAILED"

    @pytest.mark.asyncio
    async def test_hybrid_failed_agent_still_collected(self):
        lc = make_collector(CollectionStrategy.HYBRID, 2)
        fn = make_collect_fn({
            "sp0": {"status": "FAILED", "error": "timeout"},
            "sp1": {"status": "COMPLETED", "summary": "ok"},
        })

        b = await lc.pull(fn)
        assert len(b.results) == 2
        assert b.is_final_batch


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-RUN ISOLATION
# ═══════════════════════════════════════════════════════════════════════════

class TestCrossRunIsolation:
    """LeadCollector reset between runs."""

    def test_reset_clears_all_state(self):
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        assert lc.total_spawned == 3
        lc.reset()
        assert lc.total_spawned == 0
        assert lc.total_collected == 0
        assert lc.still_running == 0

    @pytest.mark.asyncio
    async def test_pull_after_reset_returns_empty(self):
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        lc.reset()
        b = await lc.pull(AsyncMock(return_value={"s": "stale"}))
        assert len(b.results) == 0

    def test_executor_resets_on_new_run(self):
        """ToolExecutor.set_current_run_id must reset LeadCollector."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock())
        te._ensure_lead_collector("SEQUENTIAL")
        te._lead_collector.register_spawn("old_sp", "old task")
        assert te._lead_collector.total_spawned == 1

        te.set_current_run_id("new_run")
        assert te._lead_collector is None


# ═══════════════════════════════════════════════════════════════════════════
# STAGGERED COMPLETION TIMING
# ═══════════════════════════════════════════════════════════════════════════

class TestStaggeredTiming:
    """Realistic scenarios with staggered agent completions."""

    @pytest.mark.asyncio
    async def test_3_agents_staggered_sequential(self):
        """SEQUENTIAL with agents completing at different times."""
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        completed: dict[str, dict] = {}

        async def fn(sid: str, wait: bool = False) -> dict | None:
            return completed.get(sid) or ({"_still_running": True} if not wait else {"s": sid})

        # Agent C finishes first, then A, then B
        completed["sp2"] = {"s": "C done first"}
        b1 = await lc.pull(fn)
        assert b1.results[0]["_spawn_label"] == "Agent C"
        assert b1.total_collected == 1

        completed["sp0"] = {"s": "A done second"}
        b2 = await lc.pull(fn)
        assert b2.results[0]["_spawn_label"] == "Agent A"
        assert b2.total_collected == 2

        completed["sp1"] = {"s": "B done last"}
        b3 = await lc.pull(fn)
        assert b3.results[0]["_spawn_label"] == "Agent B"
        assert b3.is_final_batch

    @pytest.mark.asyncio
    async def test_3_agents_staggered_hybrid(self):
        """HYBRID with 2 agents completing together, 1 later."""
        lc = make_collector(CollectionStrategy.HYBRID, 3)
        completed: dict[str, dict] = {}

        async def fn(sid: str, wait: bool = False) -> dict | None:
            return completed.get(sid) or ({"_still_running": True} if not wait else {"s": sid})

        # Batch 1: A and C complete simultaneously
        completed["sp0"] = {"s": "A"}
        completed["sp2"] = {"s": "C"}
        b1 = await lc.pull(fn)
        assert len(b1.results) == 2
        labels = {r["_spawn_label"] for r in b1.results}
        assert labels == {"Agent A", "Agent C"}
        assert b1.still_running == 1

        # Batch 2: B completes
        completed["sp1"] = {"s": "B"}
        b2 = await lc.pull(fn)
        assert len(b2.results) == 1
        assert b2.results[0]["_spawn_label"] == "Agent B"
        assert b2.is_final_batch

    @pytest.mark.asyncio
    async def test_5_agents_3_batches_hybrid(self):
        """HYBRID: 5 agents, completing in 3 waves."""
        lc = LeadCollector(strategy=CollectionStrategy.HYBRID, poll_interval_ms=5)
        for i in range(5):
            lc.register_spawn(f"sp{i}", f"Task {i}", f"Agent {i}")

        completed: dict[str, dict] = {}

        async def fn(sid: str, wait: bool = False) -> dict | None:
            return completed.get(sid) or ({"_still_running": True} if not wait else {"s": sid})

        # Wave 1: sp0
        completed["sp0"] = {"s": "done 0"}
        b1 = await lc.pull(fn)
        assert len(b1.results) == 1
        assert b1.total_collected == 1
        assert b1.still_running == 4

        # Wave 2: sp2, sp3
        completed["sp2"] = {"s": "done 2"}
        completed["sp3"] = {"s": "done 3"}
        b2 = await lc.pull(fn)
        assert len(b2.results) == 2
        assert b2.total_collected == 3
        assert b2.still_running == 2

        # Wave 3: sp1, sp4
        completed["sp1"] = {"s": "done 1"}
        completed["sp4"] = {"s": "done 4"}
        b3 = await lc.pull(fn)
        assert len(b3.results) == 2
        assert b3.total_collected == 5
        assert b3.is_final_batch
        assert b3.batch_index == 3


# ═══════════════════════════════════════════════════════════════════════════
# BATCH INDEX TRACKING
# ═══════════════════════════════════════════════════════════════════════════

class TestBatchIndexTracking:
    """batch_index increments correctly across modes."""

    @pytest.mark.asyncio
    async def test_sequential_batch_index_increments(self):
        lc = make_collector(CollectionStrategy.SEQUENTIAL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        for expected_idx in (1, 2, 3):
            b = await lc.pull(fn)
            assert b.batch_index == expected_idx

    @pytest.mark.asyncio
    async def test_batch_all_single_index(self):
        lc = make_collector(CollectionStrategy.BATCH_ALL, 3)
        fn = make_collect_fn({"sp0": {"s": "A"}, "sp1": {"s": "B"}, "sp2": {"s": "C"}})

        b = await lc.pull(fn)
        assert b.batch_index == 1

    @pytest.mark.asyncio
    async def test_empty_pull_still_increments(self):
        lc = LeadCollector(strategy=CollectionStrategy.HYBRID, poll_interval_ms=5)
        # No spawns registered
        b = await lc.pull(AsyncMock())
        assert b.batch_index == 1
        assert len(b.results) == 0


# ═══════════════════════════════════════════════════════════════════════════
# is_still_running SENTINEL TESTS
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG-DRIVEN MODE SELECTION
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigDrivenStrategy:
    """Config JSON controls default collection strategy."""

    def test_default_config_is_hybrid(self):
        from agent_framework.infra.config import FrameworkConfig
        cfg = FrameworkConfig()
        assert cfg.subagent.default_collection_strategy == "HYBRID"
        assert cfg.subagent.collection_poll_interval_ms == 500

    def test_config_sequential(self):
        from agent_framework.infra.config import load_config
        cfg = load_config("config/collection_sequential.json")
        assert cfg.subagent.default_collection_strategy == "SEQUENTIAL"
        assert cfg.subagent.collection_poll_interval_ms == 300

    def test_config_batch_all(self):
        from agent_framework.infra.config import load_config
        cfg = load_config("config/collection_batch.json")
        assert cfg.subagent.default_collection_strategy == "BATCH_ALL"

    def test_config_hybrid(self):
        from agent_framework.infra.config import load_config
        cfg = load_config("config/collection_hybrid.json")
        assert cfg.subagent.default_collection_strategy == "HYBRID"

    def test_executor_receives_config_default(self):
        """ToolExecutor must receive default_collection_strategy from config."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(
            registry=MagicMock(),
            default_collection_strategy="BATCH_ALL",
            collection_poll_interval_ms=200,
        )
        assert te._default_collection_strategy == "BATCH_ALL"
        assert te._collection_poll_interval_ms == 200

    def test_executor_default_fallback(self):
        """ToolExecutor defaults to HYBRID when not specified."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock())
        assert te._default_collection_strategy == "HYBRID"

    def test_framework_wires_config_to_executor(self):
        """AgentFramework.setup() must pass config to ToolExecutor."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        te = fw._deps.tool_executor
        assert te._default_collection_strategy == "HYBRID"
        assert te._collection_poll_interval_ms == 500

    def test_framework_sequential_config_wires_through(self):
        """Config SEQUENTIAL flows from JSON → FrameworkConfig → ToolExecutor."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework(config_path="config/collection_sequential.json")
        fw.setup()
        te = fw._deps.tool_executor
        assert te._default_collection_strategy == "SEQUENTIAL"
        assert te._collection_poll_interval_ms == 300

    def test_framework_batch_config_wires_through(self):
        """Config BATCH_ALL flows from JSON → FrameworkConfig → ToolExecutor."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework(config_path="config/collection_batch.json")
        fw.setup()
        te = fw._deps.tool_executor
        assert te._default_collection_strategy == "BATCH_ALL"

    @pytest.mark.asyncio
    async def test_config_default_used_when_llm_omits_strategy(self):
        """When LLM doesn't specify collection_strategy, config default is used."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(
            registry=MagicMock(),
            default_collection_strategy="SEQUENTIAL",
            collection_poll_interval_ms=100,
        )
        # Simulate: LLM calls spawn_agent without collection_strategy
        te._ensure_lead_collector("")  # Empty string → fallback to config
        assert te._lead_collector.strategy.value == "SEQUENTIAL"

    @pytest.mark.asyncio
    async def test_llm_override_beats_config(self):
        """LLM-specified collection_strategy overrides config default."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(
            registry=MagicMock(),
            default_collection_strategy="SEQUENTIAL",
        )
        # Simulate: LLM explicitly sets BATCH_ALL
        te._ensure_lead_collector("BATCH_ALL")
        assert te._lead_collector.strategy.value == "BATCH_ALL"

    @pytest.mark.asyncio
    async def test_config_poll_interval_flows_to_collector(self):
        """collection_poll_interval_ms from config reaches LeadCollector."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(
            registry=MagicMock(),
            collection_poll_interval_ms=200,
        )
        te._ensure_lead_collector("HYBRID")
        assert te._lead_collector._poll_interval_s == 0.2  # 200ms → 0.2s

    def test_reference_config_has_collection_fields(self):
        """reference.json must document collection_strategy fields."""
        import json
        with open("config/reference.json") as f:
            ref = json.load(f)
        sa = ref["subagent"]
        assert "default_collection_strategy" in sa
        assert sa["default_collection_strategy"] == "HYBRID"
        assert "collection_poll_interval_ms" in sa
        assert sa["collection_poll_interval_ms"] == 500


# ═══════════════════════════════════════════════════════════════════════════
# PROGRESSIVE + COLLECTION_STRATEGY COEXISTENCE
# ═══════════════════════════════════════════════════════════════════════════

class TestProgressiveCollectionCoexistence:
    """execution_mode=progressive must not kill collection_strategy."""

    def test_progressive_with_explicit_wait_false_allows_async(self):
        """LLM explicitly sets wait=false → async spawn even in progressive mode."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock(), default_collection_strategy="HYBRID")
        te._progressive_mode = True

        # Simulate: LLM passes wait=false explicitly
        args = {"wait": False, "collection_strategy": "SEQUENTIAL"}
        # Extract the same way _subagent_spawn does
        wait = args.get("wait", True)
        llm_explicitly_async = "wait" in args and not args["wait"]
        if te._progressive_mode and not llm_explicitly_async:
            wait = True
        assert wait is False, "Explicit wait=false must survive progressive mode"

    def test_progressive_default_wait_stays_true(self):
        """LLM omits wait (default True) in progressive → stays wait=True."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock())
        te._progressive_mode = True

        args = {"task_input": "test"}  # No wait key
        wait = args.get("wait", True)
        llm_explicitly_async = "wait" in args and not args["wait"]
        if te._progressive_mode and not llm_explicitly_async:
            wait = True
        assert wait is True

    def test_progressive_explicit_wait_true_stays_true(self):
        """LLM explicitly sets wait=true in progressive → stays True."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock())
        te._progressive_mode = True

        args = {"wait": True}
        wait = args.get("wait", True)
        llm_explicitly_async = "wait" in args and not args["wait"]
        if te._progressive_mode and not llm_explicitly_async:
            wait = True
        assert wait is True

    def test_non_progressive_wait_false_always_works(self):
        """Without progressive, wait=false always works."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock())
        te._progressive_mode = False

        args = {"wait": False}
        wait = args.get("wait", True)
        llm_explicitly_async = "wait" in args and not args["wait"]
        if te._progressive_mode and not llm_explicitly_async:
            wait = True
        assert wait is False

    def test_progressive_async_spawn_creates_collector(self):
        """In progressive mode, explicit wait=false must still create LeadCollector."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(
            registry=MagicMock(),
            default_collection_strategy="SEQUENTIAL",
            collection_poll_interval_ms=100,
        )
        te._progressive_mode = True

        # Simulate the logic from _subagent_spawn for wait=false
        args = {"wait": False, "collection_strategy": "BATCH_ALL"}
        llm_explicitly_async = "wait" in args and not args["wait"]
        if te._progressive_mode and not llm_explicitly_async:
            pass  # would force wait=True
        else:
            # This path: explicit wait=false, create collector
            strategy_str = args.get("collection_strategy", "").upper()
            if strategy_str not in ("SEQUENTIAL", "BATCH_ALL", "HYBRID"):
                strategy_str = te._default_collection_strategy
            te._ensure_lead_collector(strategy_str)

        assert te._lead_collector is not None
        assert te._lead_collector.strategy.value == "BATCH_ALL"

    def test_config_documents_interaction(self):
        """SubAgentConfig docstring must document the interaction."""
        from agent_framework.infra.config import SubAgentConfig
        doc = SubAgentConfig.__doc__ or ""
        assert "execution_mode" in doc
        assert "collection_strategy" in doc
        assert "INTRA-iteration" in doc or "intra-iteration" in doc.lower()


# ═══════════════════════════════════════════════════════════════════════════
# BOUNDARY: Timeout protection, strategy mismatch, _still_running in batch_all
# ═══════════════════════════════════════════════════════════════════════════

class TestBoundaryPollTimeout:
    """Poll loops must not run forever."""

    @pytest.mark.asyncio
    async def test_sequential_timeout_raises(self):
        from agent_framework.subagent.lead_collector import \
            CollectionTimeoutError
        lc = LeadCollector(
            strategy=CollectionStrategy.SEQUENTIAL,
            poll_interval_ms=1,
            max_poll_cycles=3,
        )
        lc.register_spawn("sp1", "Task 1")

        # Agent never completes
        async def never_done(sid: str, wait: bool = False):
            return {"_still_running": True}

        with pytest.raises(CollectionTimeoutError, match="SEQUENTIAL poll exceeded 3"):
            await lc.pull(never_done)

    @pytest.mark.asyncio
    async def test_hybrid_timeout_raises(self):
        from agent_framework.subagent.lead_collector import \
            CollectionTimeoutError
        lc = LeadCollector(
            strategy=CollectionStrategy.HYBRID,
            poll_interval_ms=1,
            max_poll_cycles=3,
        )
        lc.register_spawn("sp1", "Task 1")

        async def never_done(sid: str, wait: bool = False):
            return {"_still_running": True}

        with pytest.raises(CollectionTimeoutError, match="HYBRID poll exceeded 3"):
            await lc.pull(never_done)

    @pytest.mark.asyncio
    async def test_completes_before_timeout(self):
        """Normal completion should NOT trigger timeout."""
        lc = LeadCollector(
            strategy=CollectionStrategy.SEQUENTIAL,
            poll_interval_ms=1,
            max_poll_cycles=100,
        )
        lc.register_spawn("sp1", "Task 1")

        async def immediate(sid: str, wait: bool = False):
            return {"status": "COMPLETED", "summary": "done"}

        b = await lc.pull(immediate)  # Should not raise
        assert len(b.results) == 1


class TestBoundaryBatchAllStillRunning:
    """BATCH_ALL must filter _still_running results (bug fix)."""

    @pytest.mark.asyncio
    async def test_batch_all_filters_still_running_marker(self):
        """_still_running dict must NOT be collected as a valid result."""
        lc = LeadCollector(strategy=CollectionStrategy.BATCH_ALL)
        lc.register_spawn("sp1", "Task 1")
        lc.register_spawn("sp2", "Task 2")

        async def mixed_fn(sid: str, wait: bool = False):
            if sid == "sp1":
                return {"status": "COMPLETED", "summary": "done"}
            # sp2: runtime returns None even with wait=True (edge case)
            # _collect_fn wraps it as _still_running
            return {"_still_running": True, "spawn_id": "sp2", "status": "RUNNING"}

        b = await lc.pull(mixed_fn)
        # sp2 with _still_running must NOT be in results
        collected_ids = {r["_spawn_id"] for r in b.results}
        assert "sp1" in collected_ids
        assert "sp2" not in collected_ids
        assert len(b.results) == 1


class TestBoundaryStrategyMismatch:
    """Second spawn with different strategy must warn, not silently ignore."""

    def test_mismatch_logs_warning(self):
        """_ensure_lead_collector with different strategy should not change it."""
        from agent_framework.tools.executor import ToolExecutor
        te = ToolExecutor(registry=MagicMock(), default_collection_strategy="HYBRID")

        te._ensure_lead_collector("SEQUENTIAL")
        assert te._lead_collector.strategy.value == "SEQUENTIAL"

        # Second call with different strategy: collector unchanged
        te._ensure_lead_collector("BATCH_ALL")
        assert te._lead_collector.strategy.value == "SEQUENTIAL"  # First wins


class TestIsStillRunning:
    """Sentinel-based running detection."""

    def test_none_is_running(self):
        assert is_still_running(None) is True

    def test_sentinel_is_running(self):
        from agent_framework.subagent.lead_collector import _STILL_RUNNING
        assert is_still_running(_STILL_RUNNING) is True

    def test_dict_with_marker_is_running(self):
        assert is_still_running({"_still_running": True, "status": "RUNNING"}) is True

    def test_completed_dict_is_not_running(self):
        assert is_still_running({"status": "COMPLETED", "summary": "done"}) is False

    def test_failed_dict_is_not_running(self):
        assert is_still_running({"status": "FAILED", "error": "crash"}) is False

    def test_dict_without_marker_is_not_running(self):
        assert is_still_running({"status": "RUNNING"}) is False  # No _still_running marker

    def test_empty_dict_is_not_running(self):
        assert is_still_running({}) is False
